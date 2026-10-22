"""Simulator backend: serves the device page and plays the assistant's LLM ("Alexa+ role").

The browser is the real MCP client: it calls initialize / tools/list / tools/call on the MCP
server over Streamable HTTP. This backend never talks to the MCP server. It only receives the
conversation plus the tools the browser listed, asks the LLM what to do, and returns either
tool calls (which the browser then executes over MCP) or the text to speak.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from dks import config
from dks.llm import get_provider

STATIC = Path(__file__).resolve().parent / "static"
SKILL_PATH = config.APP_DIR / "skill" / "daily-korean-scene" / "SKILL.md"
MAX_MESSAGES = 30
log = logging.getLogger("dks.simulator")


def _skill_body() -> str:
    text = SKILL_PATH.read_text(encoding="utf-8")
    return re.sub(r"\A---.*?---\s*", "", text, flags=re.S)  # drop YAML front matter


SYSTEM = (config.APP_DIR / "prompts" / "orchestrator.md").read_text(encoding="utf-8").replace("{skill}", _skill_body())


def _clean_schema(schema: Any) -> Any:
    """MCP input schemas -> what Bedrock tool specs accept: collapse Optional (anyOf with null), drop titles."""
    if isinstance(schema, dict):
        s = {k: _clean_schema(v) for k, v in schema.items() if k not in ("title",)}
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
    tools = [{"name": t["name"], "description": t.get("description") or t.get("title") or "",
              "input_schema": _clean_schema(t.get("inputSchema") or {"type": "object", "properties": {}})}
             for t in body.get("tools") or []]
    if not messages:
        return JSONResponse({"error": "messages is empty"}, status_code=400)
    try:
        reply = await asyncio.to_thread(
            get_provider().converse, model=config.ORCHESTRATOR_MODEL, system=SYSTEM, messages=messages,
            tools=tools or None, max_tokens=600, temperature=0.3)
    except Exception as e:  # surfaced in the simulator log panel and the server log
        log.exception("orchestrator LLM call failed (%d messages)", len(messages))
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)
    return JSONResponse({
        "message": reply.message, "text": reply.text,
        "tool_uses": reply.tool_uses, "stop_reason": reply.stop_reason,
        "model": reply.model, "latency_ms": reply.latency_ms, "usage": reply.usage,
    })


async def sim_config(request: Request) -> JSONResponse:
    return JSONResponse({
        "mcp_url": config.env("DKS_MCP_URL", "http://127.0.0.1:8790/mcp"),
        "learner_token": config.env("DKS_DEMO_LEARNER", "demo-learner"),
        "orchestrator_model": config.ORCHESTRATOR_MODEL,
        "demo_mode": config.DEMO_MODE,
    })


async def index(request: Request) -> FileResponse:
    return FileResponse(STATIC / "index.html")


app = Starlette(routes=[
    Route("/", index),
    Route("/api/turn", turn, methods=["POST"]),
    Route("/api/config", sim_config),
    Mount("/static", StaticFiles(directory=STATIC), name="static"),
])
