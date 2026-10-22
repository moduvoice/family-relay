"""Daily Korean Scene - MCP server (spec 2025-11-25, Streamable HTTP) with MCP Apps cards.

Learner identity comes from the `Authorization: Bearer <token>` header, the way an
account-linked Alexa+ add-on receives it. The demo simulator sends `Bearer demo-learner`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from mcp.server.apps import Apps, ResourceCsp
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from . import config, state
from .cards import build_card
from .scenes import audio_path, load_scenes

SERVER_NAME = "daily-korean-scene"
UI = {
    "scene": "ui://daily-korean-scene/scene-card.html",
    "review": "ui://daily-korean-scene/review-carousel.html",
    "progress": "ui://daily-korean-scene/progress-carousel.html",
    "shop": "ui://daily-korean-scene/purchase-card.html",
    "schedule": "ui://daily-korean-scene/schedule-card.html",
}

INSTRUCTIONS = (
    "Daily Korean Scene: a 5-minute daily role-play that gets English speakers speaking Korean. "
    "Start with start_daily_scene. The device plays the characters' Korean lines; the learner answers out loud "
    "and you pass their exact words to submit_learner_turn. Mistakes go into a spaced-repetition review queue "
    "(get_review_queue) that comes back on later days."
)

apps = Apps()


# ---------------------------------------------------------------- helpers
def _learner(ctx: Context) -> str:
    auth = (ctx.headers or {}).get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    lid = re.sub(r"[^A-Za-z0-9_.-]", "", token)[:64]
    return lid or "anonymous"


def _base_url(ctx: Context) -> str:
    if config.PUBLIC_BASE_URL:
        return config.PUBLIC_BASE_URL.rstrip("/")
    req = getattr(ctx.request_context, "request", None)
    if isinstance(req, Request):
        return str(req.base_url).rstrip("/")
    return ""


def _result(text: str, data: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=data)


def _error(msg: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=msg)], is_error=True)


def _spoken(lines: list[dict[str, Any]]) -> str:
    out = []
    for ln in lines:
        if ln["speaker"] == "narrator":
            out.append(f"(narrator) {ln['en']}")
        else:
            out.append(f"{ln['speaker_role_en']}: {ln['ko']} ({ln['en']})")
    return " | ".join(out)


def _prompt_text(prompt: dict[str, Any] | None) -> str:
    if not prompt:
        return ""
    return (f" Learner's turn {prompt['turn']}/{prompt['turns_total']}: they should say in Korean "
            f"\"{prompt['task_en']}\". Hint: {prompt['hint_en']}")


# ---------------------------------------------------------------- tools
@apps.tool(
    resource_uri=UI["scene"],
    title="Start today's Korean scene",
    description=(
        "Start (or resume) the learner's daily Korean role-play scene. The device plays the characters' lines "
        "and shows the scene card; then it is the learner's turn to speak Korean. Use when the learner wants to "
        "practice Korean, do today's scene, or continue a scene. Pass scene_id only if the learner names a "
        "specific owned scene; set replay=true to redo today's finished scene."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def start_daily_scene(ctx: Context, scene_id: str | None = None, replay: bool = False) -> CallToolResult:
    try:
        r = state.start_daily_scene(_learner(ctx), _base_url(ctx), scene_id=scene_id, replay=replay)
    except state.FlowError as e:
        return _error(str(e))
    sc = r["scene"]
    if r["status"] == "locked":
        pack = r.get("pack") or {}
        return _result(f"Scene '{sc['title_en']}' is part of the '{pack.get('title_en')}' pack "
                       f"(${pack.get('price_usd')}), which the learner does not own. Offer purchase_scene_pack.", r)
    if r["status"] == "completed_today":
        return _result(f"Today's scene '{sc['title_en']}' is already done. {r['review_due']} review card(s) due. "
                       "Offer review, progress, or replay=true.", r)
    text = (f"Scene '{sc['title_en']}' ({sc['title_ko']}), day {r['day_number']}, {r['status']}. "
            f"Setting: {sc['setting_en']} The device already played: {_spoken(r['lines'])}.{_prompt_text(r['prompt'])}"
            " Do not say the Korean answer for them; invite them to try.")
    if r["review_due"]:
        text += f" Also {r['review_due']} review card(s) from earlier days are due."
    return _result(text, r)


@apps.tool(
    resource_uri=UI["scene"],
    title="Submit the learner's Korean answer",
    description=(
        "Grade what the learner just said in Korean for the current scene turn, and advance the scene. "
        "Pass the learner's words exactly as heard (Korean script), without fixing them. The result says what "
        "was wrong, the corrected sentence, and plays the next character lines. For review flashcards use "
        "submit_review_answer instead."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def submit_learner_turn(ctx: Context, answer_ko: str) -> CallToolResult:
    try:
        r = state.submit_learner_turn(_learner(ctx), answer_ko, _base_url(ctx))
    except state.FlowError as e:
        return _error(str(e))
    head = _grade_text(r["grade"])
    if r["status"] == "try_again":
        tail = " The learner gets one more try at the same line."
    elif r["status"] == "scene_complete":
        s = r["summary"]
        tail = (f" Then the device played: {_spoken(r['lines'])}. Scene complete: {s['correct_first_try']}/{s['turns']}"
                f" right first try, streak {s['streak']} day(s), {len(s['to_review'])} line(s) saved for review tomorrow.")
    else:
        tail = f" Then the device played: {_spoken(r['lines'])}.{_prompt_text(r['prompt'])}"
    if r.get("added_to_review"):
        tail += " This line was added to the review queue."
    return _result(head + tail, r)


def _grade_text(g: dict[str, Any]) -> str:
    issues = "; ".join(f"{i['type']}: {i['explanation_en']}" for i in g.get("issues", [])) or "none"
    return (f"Verdict {g['verdict']} ({g['score']}/100). Issues: {issues}. Corrected: {g['corrected_ko']}. "
            f"Coach line: {g['feedback_en']}")


@apps.tool(
    resource_uri=UI["review"],
    title="Review earlier mistakes",
    description=(
        "Start a spaced-repetition review of Korean phrases the learner got wrong on earlier days that are due "
        "today. Shows flashcards; the learner answers each out loud and you pass the answer to submit_review_answer."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def get_review_queue(ctx: Context) -> CallToolResult:
    r = state.get_review_queue(_learner(ctx))
    if r["status"] == "nothing_due":
        return _result(f"No review cards due today ({r['total_items']} in the queue, next due {r['next_due_day']}).", r)
    first = r["cards"][0]
    return _result(f"{len(r['cards'])} review card(s) due. Card 1: the learner should say \"{first['prompt_en']}\" "
                   "in Korean. Do not tell them the answer.", r)


@apps.tool(
    resource_uri=UI["review"],
    title="Answer a review flashcard",
    description=(
        "Grade the learner's spoken Korean answer to the current review flashcard (after get_review_queue) and "
        "move to the next card. Pass their words exactly as heard. Correct answers move the card to a longer "
        "interval; mistakes bring it back tomorrow."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def submit_review_answer(ctx: Context, answer_ko: str) -> CallToolResult:
    try:
        r = state.submit_review_answer(_learner(ctx), answer_ko)
    except state.FlowError as e:
        return _error(str(e))
    nxt = r.get("next_card")
    tail = (f" Next card {nxt['position']}/{nxt['total']}: learner should say \"{nxt['prompt_en']}\" in Korean."
            if nxt else f" Review finished: {r['summary']['correct']}/{r['summary']['reviewed']} correct.")
    return _result(f"{_grade_text(r['grade'])} This card comes back on {r['next_review_day']}.{tail}", r)


@apps.tool(
    resource_uri=UI["progress"],
    title="Show learning progress",
    description="Show the learner's streak, this week's practice, accuracy, review queue health, owned packs and schedule.",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def get_progress(ctx: Context) -> CallToolResult:
    r = state.get_progress(_learner(ctx))
    acc = f"{r['accuracy_first_try']}%" if r["accuracy_first_try"] is not None else "n/a"
    text = (f"Streak {r['streak']} day(s) (best {r['best_streak']}), {r['days_practiced']} scene day(s) done, "
            f"first-try accuracy {acc}, {r['review']['due_today']} review card(s) due today of {r['review']['total']}, "
            f"XP {r['xp']}.")
    if r["schedule"]:
        text += f" Next scheduled practice: {r['schedule']['next_label']}."
    return _result(text, r)


@apps.tool(
    resource_uri=UI["shop"],
    title="Browse scene packs",
    description="List the scene packs the learner can buy, with price, scenes and whether they own them.",
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def browse_scene_packs(ctx: Context) -> CallToolResult:
    r = state.browse_scene_packs(_learner(ctx))
    def desc(p: dict[str, Any]) -> str:
        ready = sum(1 for s in p["scenes"] if s["available"])
        sale = "owned" if p["owned"] else (f"${p['price_usd']}" if ready else "not on sale yet")
        return f"{p['title_en']} (id {p['pack_id']}, {sale}, {ready} of {p['scene_count']} scenes released so far)"
    text = "Scene packs: " + "; ".join(desc(p) for p in r["packs"])
    return _result(text, {"view": "browse", **r})


@apps.tool(
    resource_uri=UI["shop"],
    title="Buy a scene pack",
    description=(
        "Buy a scene pack in two steps. Step 1: call with the pack (id or name) and confirm=false; this shows a "
        "confirmation card with the price and returns a purchase_token. Then ask the learner to confirm out loud. "
        "Step 2: only after the learner clearly says yes, call again with confirm=true and the purchase_token. "
        "If they decline, call with cancel=true and the purchase_token. Never confirm without the learner's yes."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
)
def purchase_scene_pack(ctx: Context, pack: str, confirm: bool = False, purchase_token: str | None = None,
                        cancel: bool = False) -> CallToolResult:
    try:
        r = state.purchase_scene_pack(_learner(ctx), pack, confirm, purchase_token, cancel)
    except state.FlowError as e:
        return _error(str(e))
    p = r["pack"]
    status = r["status"]
    if status == "awaiting_confirmation":
        text = (f"Confirmation needed: '{p['title_en']}' for ${r['price_usd']} (demo wallet, no real charge). "
                f"purchase_token={r['purchase_token']}. Ask the learner to confirm before calling again with confirm=true.")
    elif status == "purchased":
        ready = [s["title_en"] for s in p["scenes"] if s["available"]]
        text = (f"Purchased '{p['title_en']}' (order {r['order_id']}). Unlocked now: {', '.join(ready) or 'none yet'}; "
                f"{len(p['scenes']) - len(ready)} more coming soon.")
    elif status == "cancelled":
        text = f"Purchase of '{p['title_en']}' cancelled. Nothing was charged."
    else:
        text = f"The learner already owns '{p['title_en']}'."
    return _result(text, {"view": "purchase", **r})


@apps.tool(
    resource_uri=UI["schedule"],
    title="Schedule practice",
    description=(
        "Set the learner's recurring practice reminder. days: list like ['mon','wed','fri'] or ['weekdays'] / "
        "['weekends'] / ['daily']. time_local: 24h 'HH:MM' in the learner's timezone. timezone: optional IANA name."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def schedule_practice(ctx: Context, days: list[str], time_local: str, timezone: str | None = None,
                      minutes: int = 5) -> CallToolResult:
    try:
        r = state.schedule_practice(_learner(ctx), days, time_local, timezone, minutes)
    except state.FlowError as e:
        return _error(str(e))
    s = r["schedule"]
    return _result(f"Practice scheduled on {', '.join(s['days'])} at {s['time_local']} ({s['timezone']}), "
                   f"{s['minutes']} minutes. Next: {s['next_label']}.", r)


# ---------------------------------------------------------------- server & routes
def _csp() -> ResourceCsp:
    domains = [config.PUBLIC_BASE_URL] if config.PUBLIC_BASE_URL else ["http://127.0.0.1:8790", "http://localhost:8790"]
    return ResourceCsp(resource_domains=domains, connect_domains=[])


for key, uri in UI.items():
    apps.add_html_resource(uri, build_card(key), name=f"{key}-card", title=f"Daily Korean Scene {key} card",
                           csp=_csp(), prefers_border=False)

mcp = MCPServer(SERVER_NAME, title="Daily Korean Scene", version="1.0.0", instructions=INSTRUCTIONS,
                website_url="https://github.com/moduvoice/daily-korean-scene", extensions=[apps])


def _learner_from_request(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    return re.sub(r"[^A-Za-z0-9_.-]", "", token)[:64] or "anonymous"


@mcp.custom_route("/audio/{scene_id}/{file_name}", methods=["GET"])
async def audio(request: Request) -> Response:
    scene_id = request.path_params["scene_id"]
    stem = Path(request.path_params["file_name"]).stem
    scene = load_scenes().get(scene_id)
    line = next((ln for ln in scene.lines if ln["id"] == stem), None) if scene else None
    path = audio_path(scene_id, line) if line else None
    if not path:
        return Response(status_code=404)
    return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "public, max-age=86400"})


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> Response:
    return JSONResponse({"ok": True, "scenes": sorted(load_scenes())})


@mcp.custom_route("/demo/clock", methods=["GET", "POST"])
async def demo_clock(request: Request) -> Response:
    """Demo-only time travel: POST {"advance_days": 1} or {"reset": true}. Disabled unless DKS_DEMO_MODE=1."""
    if not config.DEMO_MODE:
        return Response(status_code=404)
    lid = _learner_from_request(request)
    if request.method == "POST":
        body = json.loads((await request.body()) or b"{}")
        if body.get("reset"):
            state.reset_learner(lid)
        elif body.get("advance_days"):
            state.advance_days(lid, int(body["advance_days"]))
    return JSONResponse({"learner_id": lid, **state.clock_view(state.profile(lid))})


def _transport_security() -> TransportSecuritySettings:
    if config.env_bool("DKS_DNS_REBINDING_PROTECTION", True):
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*"] + config.ALLOWED_ORIGINS,
        )
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def create_app():
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=config.MCP_STATELESS,
        json_response=config.MCP_JSON_RESPONSE,
        transport_security=_transport_security(),
    )
    origins = config.ALLOWED_ORIGINS or ["http://127.0.0.1:8791", "http://localhost:8791"]
    return CORSMiddleware(
        app, allow_origins=origins, allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"], expose_headers=["Mcp-Session-Id", "MCP-Protocol-Version"], max_age=600,
    )


app = create_app()
