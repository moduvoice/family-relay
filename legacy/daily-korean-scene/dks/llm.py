"""LLM provider abstraction.

Messages and tool specs use the Bedrock Converse shape
({"role", "content": [{"text"} | {"toolUse"} | {"toolResult"}]}) as the neutral format.
A new provider only has to translate that shape; Bedrock is the one we ship and test.

Model strings may list several Bedrock endpoints, comma-separated, each "region|modelId"
(region defaults to DKS_BEDROCK_REGION). Bedrock request-per-minute quotas are per region and
per inference profile, so a list spreads load over separate quotas: calls rotate round-robin
and a ThrottlingException moves on to the next endpoint. Any other error is raised as is.
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import config


@dataclass
class LlmReply:
    text: str
    tool_uses: list[dict[str, Any]]           # [{"toolUseId", "name", "input"}]
    message: dict[str, Any]                    # assistant message to append to the conversation
    stop_reason: str
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    latency_ms: int = 0


class LlmProvider(Protocol):
    def converse(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        force_tool: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> LlmReply: ...


class BedrockProvider:
    def __init__(self, default_region: str, profile: str = ""):
        import boto3

        self._session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        self._default_region = default_region
        self._clients: dict[str, Any] = {}
        self._rr: dict[str, itertools.count] = {}
        self._lock = threading.Lock()

    def _client(self, region: str):
        from botocore.config import Config

        with self._lock:
            if region not in self._clients:
                self._clients[region] = self._session.client(
                    "bedrock-runtime", region_name=region,
                    config=Config(read_timeout=60, retries={"max_attempts": 1, "mode": "standard"}))
            return self._clients[region]

    def _endpoints(self, model: str) -> list[tuple[str, str]]:
        eps = []
        for part in (p.strip() for p in model.split(",") if p.strip()):
            region, _, mid = part.rpartition("|")
            eps.append((region or self._default_region, mid))
        with self._lock:
            start = next(self._rr.setdefault(model, itertools.count()))
        k = start % len(eps)
        return eps[k:] + eps[:k]

    def converse(self, *, model, system, messages, tools=None, force_tool=None, max_tokens=1024, temperature=0.2):
        kwargs: dict[str, Any] = {
            "system": [{"text": system}],
            "messages": messages,
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if tools:
            tool_config: dict[str, Any] = {
                "tools": [{"toolSpec": {"name": t["name"], "description": t.get("description", ""),
                                        "inputSchema": {"json": t["input_schema"]}}} for t in tools]
            }
            if force_tool:
                tool_config["toolChoice"] = {"tool": {"name": force_tool}}
            kwargs["toolConfig"] = tool_config
        last_exc: Exception | None = None
        for region, mid in self._endpoints(model):
            client = self._client(region)
            t0 = time.time()
            try:
                resp = client.converse(modelId=mid, **kwargs)
            except client.exceptions.ThrottlingException as e:
                last_exc = e
                continue
            latency = int((time.time() - t0) * 1000)
            msg = resp["output"]["message"]
            text = "".join(b.get("text", "") for b in msg["content"])
            uses = [b["toolUse"] for b in msg["content"] if "toolUse" in b]
            return LlmReply(text=text.strip(), tool_uses=uses, message=msg, stop_reason=resp.get("stopReason", ""),
                            usage=resp.get("usage", {}), model=f"{region}|{mid}", latency_ms=latency)
        raise last_exc  # every endpoint throttled


_provider: LlmProvider | None = None


def get_provider() -> LlmProvider:
    global _provider
    if _provider is None:
        if config.LLM_PROVIDER == "bedrock":
            _provider = BedrockProvider(config.BEDROCK_REGION, config.AWS_PROFILE)
        else:
            raise ValueError(f"unknown DKS_LLM_PROVIDER={config.LLM_PROVIDER!r} (implement LlmProvider to add one)")
    return _provider
