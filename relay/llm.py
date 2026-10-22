"""LLM provider abstraction for the assistant ("Alexa+ role") loop.

The conversation uses one neutral shape (Bedrock Converse style):
  {"role": "user"|"assistant", "content": [{"text"} | {"toolUse": {toolUseId, name, input}} |
                                           {"toolResult": {toolUseId, status, content: [{"text"}]}}]}
Every provider takes (system, messages, tools) and returns an LlmReply whose `message` is appended as is.

Providers (FR_LLM_PROVIDER):
  gptproxy  OpenAI-compatible /v1/chat/completions (default; local gpt-proxy, model gpt-6-luna). It has no
            function calling, so tools are described in the system prompt and the reply is a JSON decision
            constrained by response_format=json_schema.
  nova      Amazon Bedrock Converse with native tool use (Amazon Nova Micro / Lite).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import config


@dataclass
class LlmReply:
    text: str
    tool_uses: list[dict[str, Any]]           # [{"toolUseId", "name", "input"}]
    message: dict[str, Any]                    # assistant message to append to the conversation
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    latency_ms: int = 0


class LlmProvider(Protocol):
    def converse(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                 max_tokens: int = 800, audio: dict[str, Any] | None = None) -> LlmReply: ...


# ---------------------------------------------------------------- OpenAI-compatible (gpt-proxy)
DECISION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "tool_calls": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"name": {"type": "string"}, "arguments_json": {"type": "string"}},
            "required": ["name", "arguments_json"]}},
        "say": {"type": "string"},
    },
    "required": ["tool_calls", "say"],
}

TOOL_PROTOCOL = """
## Tools
You act by returning JSON: {"tool_calls": [{"name": ..., "arguments_json": "<JSON object as a string>"}], "say": ...}.
- To use tools, list them in tool_calls (several at once is fine) and leave say empty. Their results come back
  in the next user turn as TOOL_RESULT lines, then you decide again.
- When you have what you need, return an empty tool_calls list and put your spoken reply in say.
Available tools (name, what it does, JSON schema of its arguments):
"""


def _render_for_chat(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for m in messages:
        parts = []
        for c in m["content"]:
            if "text" in c and c["text"]:
                parts.append(c["text"])
            elif "toolUse" in c:
                tu = c["toolUse"]
                parts.append(f"TOOL_CALL {tu['name']} {json.dumps(tu['input'], ensure_ascii=False)}")
            elif "toolResult" in c:
                tr = c["toolResult"]
                txt = " ".join(x.get("text", "") for x in tr["content"])
                parts.append(f"TOOL_RESULT ({tr.get('status', 'success')}) {txt}")
        if parts:
            out.append({"role": m["role"], "content": "\n".join(parts)})
    return out


class OpenAICompatProvider:
    def __init__(self, base_url: str, model: str, api_key: str = "", effort: str = "low"):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model, self.api_key, self.effort = model, api_key, effort

    def converse(self, *, system, messages, tools, max_tokens=800, audio=None):  # audio: {"mime","b64"}; text-only model ignores it
        catalog = "\n".join(f"- {t['name']}: {t.get('description', '')}\n  args: {json.dumps(t['input_schema'], ensure_ascii=False)}"
                            for t in tools)
        body = {"model": self.model, "reasoning_effort": self.effort,
                "messages": [{"role": "system", "content": system + "\n" + TOOL_PROTOCOL + catalog}] + _render_for_chat(messages),
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "decision", "strict": True, "schema": DECISION_SCHEMA}}}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=json.dumps(body, ensure_ascii=False).encode(), headers=headers)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"LLM HTTP {e.code}: {e.read()[:300].decode('utf-8', 'ignore')}") from None
        latency = int((time.time() - t0) * 1000)
        raw = resp["choices"][0]["message"]["content"]
        # the decision is the first JSON object; gpt-6-luna sometimes keeps writing after it (an imitated tool
        # call and an invented TOOL_RESULT) - that tail is dropped and reported in usage, never acted on
        try:
            d, end = json.JSONDecoder().raw_decode(raw.lstrip())
        except json.JSONDecodeError:
            raise RuntimeError(f"LLM returned non-JSON despite json_schema: {raw[:200]}") from None
        tail = raw.lstrip()[end:].strip()
        known = {t["name"] for t in tools}
        uses = []
        for c in d.get("tool_calls") or []:
            if c["name"] not in known:
                raise RuntimeError(f"LLM chose unknown tool {c['name']!r}")
            args = json.loads(c["arguments_json"] or "{}")
            uses.append({"toolUseId": "tu_" + uuid.uuid4().hex[:10], "name": c["name"], "input": args})
        say = (d.get("say") or "").strip()
        content = ([{"text": say}] if say and not uses else []) + [{"toolUse": u} for u in uses]
        usage = dict(resp.get("usage", {}))
        if tail:
            usage["dropped_tail_chars"] = len(tail)
        return LlmReply(text=say if not uses else "", tool_uses=uses, message={"role": "assistant", "content": content or [{"text": say}]},
                        usage=usage, model=resp.get("model", self.model), latency_ms=latency)


# ---------------------------------------------------------------- Amazon Bedrock (Nova)
class NovaProvider:
    def __init__(self, region: str, model: str, profile: str = ""):
        import boto3

        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        self._client = session.client("bedrock-runtime", region_name=region)
        self.model = model

    def converse(self, *, system, messages, tools, max_tokens=800, audio=None):  # audio: {"mime","b64"}; text-only model ignores it
        kwargs: dict[str, Any] = {"modelId": self.model, "system": [{"text": system}], "messages": messages,
                                  "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.3}}
        if tools:
            kwargs["toolConfig"] = {"tools": [{"toolSpec": {"name": t["name"], "description": t.get("description", ""),
                                                            "inputSchema": {"json": t["input_schema"]}}} for t in tools]}
        t0 = time.time()
        resp = self._client.converse(**kwargs)
        msg = resp["output"]["message"]
        text = "".join(b.get("text", "") for b in msg["content"]).strip()
        uses = [b["toolUse"] for b in msg["content"] if "toolUse" in b]
        return LlmReply(text=text if not uses else "", tool_uses=uses, message=msg, usage=resp.get("usage", {}),
                        model=self.model, latency_ms=int((time.time() - t0) * 1000))


_provider: LlmProvider | None = None


def get_provider() -> LlmProvider:
    global _provider
    if _provider is None:
        if config.LLM_PROVIDER == "gptproxy":
            _provider = OpenAICompatProvider(config.OPENAI_BASE_URL, config.OPENAI_MODEL, config.OPENAI_API_KEY,
                                             config.OPENAI_EFFORT)
        elif config.LLM_PROVIDER == "nova":
            _provider = NovaProvider(config.NOVA_REGION, config.NOVA_MODEL, config.AWS_PROFILE)
        else:
            raise ValueError(f"unknown FR_LLM_PROVIDER={config.LLM_PROVIDER!r} (gptproxy | nova)")
    return _provider
