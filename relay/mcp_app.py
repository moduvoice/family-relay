"""Family Relay - MCP server (spec 2025-11-25, Streamable HTTP) with MCP Apps cards.

Household identity: `Authorization: Bearer <household token>` (stand-in for Alexa account linking).
Speaker identity: the device posts each utterance's audio to /speaker/observe on this server and the speaker
module decides who spoke. The host names that observation in the tools/call request
`_meta["dev.familyrelay/observation"]`, so tools act for the voice that was actually heard - the LLM never
supplies an identity, and no one is ever asked who they are.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Any

from mcp.server.apps import Apps, ResourceCsp
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import config, state
from .cards import build_card

SERVER_NAME = "family-relay"
META_OBS = "dev.familyrelay/observation"
UI = {
    "inbox": "ui://family-relay/message-card.html",
    "timeline": "ui://family-relay/family-timeline.html",
    "speaker": "ui://family-relay/voice-status.html",
}
INSTRUCTIONS = (
    "Family Relay passes short spoken messages between family members on a shared device, by voice alone. "
    "Messages live for 24 hours. Each tool acts for the voice the device just heard. When the voice clearly "
    "belongs to someone with waiting messages, pass them on alongside whatever they asked for."
)

apps = Apps()


# ---------------------------------------------------------------- helpers
def _household(headers: Any) -> str:
    auth = (headers or {}).get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    return re.sub(r"[^A-Za-z0-9_.-]", "", token)[:64] or "anonymous"


def _obs(ctx: Context, h: str) -> dict[str, Any] | None:
    meta = getattr(ctx.request_context, "meta", None)
    if meta is not None and not isinstance(meta, dict):
        meta = meta.model_dump(by_alias=True, exclude_none=True) if hasattr(meta, "model_dump") else dict(meta)
    return state.get_observation(h, (meta or {}).get(META_OBS))


def _result(text: str, data: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=data)


def _error(msg: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=msg)], is_error=True)


def _msg_line(m: dict[str, Any]) -> str:
    cond = f" (condition: {m['condition']})" if m.get("condition") else ""
    lang = f" [said in {m['lang']}]" if m.get("lang") else ""
    who = m["sender_name"] or "someone in the family"
    return f"{m['kind']} from {who} at {m['created_label']}: \"{m['text']}\"{cond}{lang}"


# ---------------------------------------------------------------- tools
@apps.tool(
    resource_uri=UI["inbox"],
    title="Leave a message for a family member",
    description=(
        "Save a message for someone in the household; it is passed on the next time their voice is clearly "
        "heard. recipient: the name or family role the speaker used ('Sunhee', 'Grandma', 'Dad'). message: what "
        "to pass on, in the speaker's words, without 'tell X'. condition: optional free text ('when she gets "
        "home'). deliver_after_local: optional 'HH:MM' local time before which it is held. Lives 24 hours."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def leave_message(ctx: Context, recipient: str, message: str, condition: str | None = None,
                  deliver_after_local: str | None = None) -> CallToolResult:
    h = _household(ctx.headers)
    try:
        r = state.leave_message(h, _obs(ctx, h), recipient, message, condition, deliver_after_local)
    except state.RelayError as e:
        return _error(str(e))
    m = r["message"]
    return _result(f"Saved for {m['recipient_name']}; passed on when their voice is heard, gone at {m['expires_label']} "
                   "if unheard.", {"view": "saved", **r})


@apps.tool(
    resource_uri=UI["inbox"],
    title="Pass on waiting messages",
    description=(
        "Hand over the messages waiting for the person speaking now. Call it in the same step as their own "
        "request whenever the turn says messages are waiting. Weave each one into your answer as 'By the way, "
        "<sender> asked me to tell you ...', in the listener's language. If it returns nothing, answer their "
        "request as usual."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
)
def get_pending_messages(ctx: Context) -> CallToolResult:
    h = _household(ctx.headers)
    r = state.deliver_pending(h, _obs(ctx, h))
    if r["status"] == "nothing":
        return _result("Nothing to pass on right now.", {"view": "inbox", **r})
    lines = "; ".join(_msg_line(m) for m in r["messages"])
    lang = r["recipient"].get("language")
    text = f"Pass these to {r['recipient']['name']}: {lines}. Each sender gets a delivery receipt on their phone."
    if lang:
        text += f" {r['recipient']['name']} talks to the device in {lang}; say it in that language."
    return _result(text, {"view": "inbox", **r})


@apps.tool(
    resource_uri=UI["inbox"],
    title="Reply to a message",
    description=(
        "Send the speaker's reply back to whoever sent them their latest message (or message_id). reply: their "
        "words, without 'tell him'. It reaches the sender the next time they are heard or ask about it."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def reply_to_message(ctx: Context, reply: str, message_id: str | None = None) -> CallToolResult:
    h = _household(ctx.headers)
    try:
        r = state.reply_to_message(h, _obs(ctx, h), reply, message_id)
    except state.RelayError as e:
        return _error(str(e))
    if r["status"] == "not_settled":
        return _result("Reply not recorded this turn; respond naturally and it can be repeated on the next turn.",
                       {"view": "inbox", "status": "nothing", "messages": [], "clock": r["clock"]})
    return _result(f"Reply saved for {r['reply']['recipient_name']}: \"{r['reply']['text']}\".", {"view": "reply", **r})


@apps.tool(
    resource_uri=UI["inbox"],
    title="Ask about a message you sent",
    description=(
        "Answer questions like 'Did Sunhee eat?' or 'Did Grandma get my message?': returns the speaker's recent "
        "messages to that person, when they were heard, and any replies (which count as passed on now). Use the "
        "replies to answer the actual question."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def ask_status(ctx: Context, about: str) -> CallToolResult:
    h = _household(ctx.headers)
    r = state.ask_status(h, _obs(ctx, h), about)
    if r["status"] == "not_settled":
        return _result("No update to share on this turn; answer briefly that there is no news yet.", {"view": "status", **r})
    if r["status"] == "no_messages":
        return _result(f"No messages from {r['asker']} to {r['about']} in the last 24 hours.", {"view": "status", **r})
    parts = []
    for m in r["messages"]:
        heard = f"heard at {m['delivered_label']}" if m["delivered_at"] else "not heard yet"
        reps = "; ".join(f"reply at {x['created_label']}: \"{x['text']}\"" for x in m["reply_messages"]) or "no reply"
        parts.append(f"message \"{m['text']}\" left {m['created_label']}, {heard}, {reps}")
    return _result(f"About {r['about']}: " + " | ".join(parts), {"view": "status", **r})


@apps.tool(
    resource_uri=UI["timeline"],
    title="Show the family's 24-hour memory",
    description="Show what the household relay remembers right now: live messages and replies, what fades soon, "
                "recent deliveries and phone notifications. Message text is shown only to its sender or recipient.",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def get_family_memory(ctx: Context) -> CallToolResult:
    h = _household(ctx.headers)
    r = state.family_memory(h, _obs(ctx, h))
    soon = ", ".join(f"{i['sender_name'] or 'someone'} to {i['recipient_name']} in {i['expires_in_min']} min"
                     for i in r["expiring_soon"]) or "none"
    return _result(f"{len(r['live'])} live item(s); fading within 3 h: {soon}; {len(r['expired'])} recently forgotten "
                   "(text erased).", r)


@apps.tool(
    resource_uri=UI["speaker"],
    title="Show voice recognition status",
    description="Show the voices the device has learned on its own from everyday use, their names, how sure it is "
                "about the current speaker, and discarded noise.",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def get_speaker_status(ctx: Context) -> CallToolResult:
    h = _household(ctx.headers)
    r = state.speaker_status(h, _obs(ctx, h))
    names = ", ".join(f"{v['name'] or 'unnamed'} ({v['utterances']})" for v in r["voices"]) or "none yet"
    return _result(f"Learned voices (utterances): {names}.", {"view": "status", **r})


# ---------------------------------------------------------------- server & routes
for key, uri in UI.items():
    apps.add_html_resource(uri, build_card(key), name=f"{key}-card", title=f"Family Relay {key} card",
                           csp=ResourceCsp(resource_domains=[], connect_domains=[]), prefers_border=False)

mcp = MCPServer(SERVER_NAME, title="Family Relay", version="1.0.0", instructions=INSTRUCTIONS,
                website_url="https://github.com/moduvoice/family-relay", extensions=[apps])


@mcp.custom_route("/speaker/observe", methods=["POST"])
async def speaker_observe(request: Request) -> Response:
    """Raw utterance audio in the body (audio/webm, audio/wav ...). Headers: X-FR-Lang, X-FR-Source
    (mic|synthetic), X-FR-TS (epoch s), X-FR-Transcript (URL-encoded, optional), and demo-only
    X-FR-Demo-Voice / X-FR-Demo-Clarity / X-FR-Demo-Name for the stand-in module."""
    h = _household(request.headers)
    audio = await request.body()
    hd = request.headers
    hint = None
    if config.DEMO_MODE and hd.get("x-fr-demo-voice") is not None:
        hint = {"voice": hd.get("x-fr-demo-voice"), "clarity": hd.get("x-fr-demo-clarity", "clear"),
                "name": urllib.parse.unquote(hd.get("x-fr-demo-name", ""))}
    transcript = urllib.parse.unquote(hd.get("x-fr-transcript", "")) or None
    t0 = time.time()
    obs = state.observe(h, audio, float(hd.get("x-fr-ts") or time.time()), hd.get("x-fr-lang"),
                        hd.get("content-type", ""), hd.get("x-fr-source", "mic"), transcript, hint)
    obs["module_ms"] = int((time.time() - t0) * 1000)
    return JSONResponse(obs)


@mcp.custom_route("/speaker/voices", methods=["GET"])
async def speaker_voices(request: Request) -> Response:
    return JSONResponse(state.speaker_status(_household(request.headers), None))


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> Response:
    return JSONResponse({"ok": True, "speaker_module": config.SPEAKER_MODULE})


@mcp.custom_route("/demo/clock", methods=["GET", "POST"])
async def demo_clock(request: Request) -> Response:
    """Demo-only: POST {"advance_minutes": n} | {"set_local": "HH:MM"} | {"reset": true}."""
    if not config.DEMO_MODE:
        return Response(status_code=404)
    h = _household(request.headers)
    if request.method == "POST":
        body = json.loads((await request.body()) or b"{}")
        if body.get("reset"):
            state.reset_household(h)
        if body.get("advance_minutes"):
            state.advance_clock(h, int(body["advance_minutes"]))
        if body.get("set_local"):
            state.set_clock(h, body["set_local"])
    return JSONResponse(state.clock_view(h))


def _transport_security() -> TransportSecuritySettings:
    if config.env_bool("FR_DNS_REBINDING_PROTECTION", True):
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=["127.0.0.1:*", "localhost:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*"] + config.ALLOWED_ORIGINS)
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def create_app():
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=config.MCP_STATELESS,
                                  json_response=config.MCP_JSON_RESPONSE, transport_security=_transport_security())
    origins = config.ALLOWED_ORIGINS or ["http://127.0.0.1:8791", "http://localhost:8791"]
    return CORSMiddleware(app, allow_origins=origins, allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                          allow_headers=["*"], expose_headers=["Mcp-Session-Id", "MCP-Protocol-Version"], max_age=600)


app = create_app()
