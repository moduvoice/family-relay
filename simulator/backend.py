"""Simulator backend: serves the device page and plays the assistant's LLM ("Alexa+ role").

The browser is the real MCP client (initialize / tools/list / tools/call over Streamable HTTP) and posts each
utterance's audio to the MCP server's /speaker/observe. This backend never talks to the MCP server: it gets
the conversation and the browser's tool list, asks the configured LLM provider what to do, and returns tool
calls (executed by the browser over MCP) or the text to speak.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from relay import config
from relay.llm import get_provider
from relay.voice.stt import get_stt
from relay.voice.tts import TtsUnsupported, get_tts

STATIC = Path(__file__).resolve().parent / "static"
SKILL_PATH = config.APP_DIR / "skill" / "family-relay" / "SKILL.md"
MAX_MESSAGES = 40
log = logging.getLogger("relay.simulator")


def _skill_body() -> str:
    return re.sub(r"\A---.*?---\s*", "", SKILL_PATH.read_text(encoding="utf-8"), flags=re.S)


SYSTEM = (config.APP_DIR / "prompts" / "orchestrator.md").read_text(encoding="utf-8").replace("{skill}", _skill_body())


def _clean_schema(schema: Any) -> Any:
    """MCP input schemas -> plain JSON schema every provider accepts: collapse Optional (anyOf with null), drop titles."""
    if isinstance(schema, dict):
        s = {k: _clean_schema(v) for k, v in schema.items() if k != "title"}
        any_of = s.get("anyOf")
        if isinstance(any_of, list):
            non_null = [x for x in any_of if x.get("type") != "null"]
            if len(non_null) == 1:
                s.pop("anyOf")
                s.update(non_null[0])
        return s
    if isinstance(schema, list):
        return [_clean_schema(x) for x in schema]
    return schema


def _trim(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the tail of the conversation, starting at a plain user message (never at a toolResult)."""
    if len(messages) <= MAX_MESSAGES:
        return messages
    tail = messages[-MAX_MESSAGES:]
    for i, m in enumerate(tail):
        if m["role"] == "user" and all("text" in c for c in m["content"]):
            return tail[i:]
    return tail[-1:]


async def turn(request: Request) -> JSONResponse:
    body = await request.json()
    messages = _trim(copy.deepcopy(body.get("messages") or []))
    if not messages:
        return JSONResponse({"error": "messages is empty"}, status_code=400)
    tools = [{"name": t["name"], "description": t.get("description") or t.get("title") or "",
              "input_schema": _clean_schema(t.get("inputSchema") or {"type": "object", "properties": {}})}
             for t in body.get("tools") or []]
    audio = body.get("audio") if config.LLM_AUDIO_INPUT else None
    try:
        reply = await asyncio.to_thread(get_provider().converse, system=SYSTEM, messages=messages, tools=tools,
                                        audio=audio)
    except Exception as e:  # surfaced in the simulator log panel and the server log
        log.exception("assistant LLM call failed (%d messages)", len(messages))
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)
    return JSONResponse({"message": reply.message, "text": reply.text, "tool_uses": reply.tool_uses,
                         "model": reply.model, "provider": config.LLM_PROVIDER,
                         "latency_ms": reply.latency_ms, "usage": reply.usage})


async def stt(request: Request) -> Response:
    """Raw utterance audio in, {"text", "lang", "engine", "ms"} out (our STT module)."""
    engine = get_stt()
    if engine is None:
        return JSONResponse({"engine": "browser", "error": "server STT disabled (FR_STT_ENGINE=browser)"}, status_code=409)
    audio = await request.body()
    try:
        r = await asyncio.to_thread(engine.transcribe, audio, request.headers.get("content-type", ""),
                                    request.headers.get("x-lang"), urllib.parse.unquote(request.headers.get("x-context", "")))
    except Exception as e:
        log.exception("STT failed")
        return JSONResponse({"engine": engine.name, "error": f"{type(e).__name__}: {e}"}, status_code=502)
    return JSONResponse({**r, "audio_bytes": len(audio)})


async def tts(request: Request) -> Response:
    """{"text", "lang"} in, audio out (our TTS module); 409 when the browser should speak instead."""
    engine = get_tts()
    if engine is None:
        return JSONResponse({"engine": "browser"}, status_code=409)
    body = await request.json()
    try:
        r = await asyncio.to_thread(engine.synthesize, body["text"], body.get("lang") or "en-US")
    except TtsUnsupported as e:   # e.g. Piper is English-only: the browser voice speaks this one
        return JSONResponse({"engine": engine.name, "error": str(e)}, status_code=409)
    except Exception as e:
        log.exception("TTS failed")
        return JSONResponse({"engine": engine.name, "error": f"{type(e).__name__}: {e}"}, status_code=502)
    return Response(r["audio"], media_type=r["mime"], headers={"X-Engine": r["engine"], "X-Ms": str(r["ms"])})


async def sim_config(request: Request) -> JSONResponse:
    return JSONResponse({"mcp_url": config.env("FR_MCP_URL", "http://127.0.0.1:8790/mcp"),
                         "household_token": config.env("FR_DEMO_HOUSEHOLD", "demo-family"),
                         "provider": config.LLM_PROVIDER, "demo_mode": config.DEMO_MODE,
                         "send_audio_to_llm": config.LLM_AUDIO_INPUT,
                         "stt_engine": config.STT_ENGINE, "profile": config.PROFILE,
                         "tts_engine": config.TTS_ENGINE,
                         "has_clips": (STATIC / "clips" / "manifest.json").exists()})


async def index(request: Request) -> FileResponse:
    return FileResponse(STATIC / "index.html")


app = Starlette(routes=[
    Route("/", index),
    Route("/api/turn", turn, methods=["POST"]),
    Route("/api/config", sim_config),
    Route("/api/stt", stt, methods=["POST"]),
    Route("/api/tts", tts, methods=["POST"]),
    Mount("/static", StaticFiles(directory=STATIC), name="static"),
])
