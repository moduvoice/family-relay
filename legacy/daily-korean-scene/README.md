# Daily Korean Scene - an Alexa+ add-on for speaking Korean

Five minutes a day, one real-life scene. A barista asks what you'd like, a taxi driver asks where to -
and you answer **out loud, in Korean**. The add-on grades what you said (meaning, grammar and politeness
level), shows exactly what to fix, and brings yesterday's mistakes back with spaced repetition.

Built for the Amazon Developer Hackathon, Alexa+ track, as a **self-hosted MCP server**
(MCP spec 2025-11-25, Streamable HTTP) plus a **web simulator that is a real MCP client** standing in for
Alexa+ (the Alexa+ add-on toolkit is partner-only).

## What it does

| Alexa+ "creative" item | How it is implemented |
|---|---|
| State across sessions | Per-learner progress, streak, XP and a Leitner spaced-repetition queue (1/2/4/7/15/30 days). Mistakes from today's scene come back tomorrow. SQLite locally, DynamoDB when deployed (same single-table shape). |
| MCP Apps cards & carousels | 5 `ui://` resources (`text/html;profile=mcp-app`) bound via `_meta.ui.resourceUri`: scene dialogue card (Korean / romanization / English, mistakes underlined, fixes highlighted), review flashcard carousel, progress carousel, purchase confirmation card, schedule card. 768x480 base canvas; inline, fullscreen and voice-only display modes. |
| Purchasing | Scene packs with a mandatory two-step confirmation (`purchase_token`, 10-minute expiry, explicit yes). Card buttons and voice both work. Demo wallet - no real charge. |
| Scheduling | Recurring practice reminder (days + local time + IANA timezone), next occurrence computed on the learner's clock. |
| Agent Skill | [`skill/daily-korean-scene/SKILL.md`](skill/daily-korean-scene/SKILL.md) - the routine an assistant follows. The simulator's assistant loads it into its system prompt. |
| Agentic, not single-turn | The assistant LLM chooses tools from `tools/list`, chains them (e.g. progress -> scene), and grading is a second LLM with structured output. |

## Architecture

```
 Browser (simulator page)                         Python
 ┌───────────────────────────────┐   /api/turn   ┌──────────────────────────┐
 │ Echo-style 768x480 screen      │ ────────────▶ │ simulator/backend.py      │  assistant LLM ("Alexa+ role")
 │ MCP client (mcp-client.js)     │ ◀──────────── │  Bedrock Claude Haiku 4.5 │  picks tools / writes the reply
 │ MCP Apps host (iframe bridge)  │               └──────────────────────────┘
 │ Web Speech in/out, log panel   │  JSON-RPC over Streamable HTTP
 │                                │ ────────────▶ ┌──────────────────────────┐
 └───────────────────────────────┘               │ dks/mcp_app.py  (/mcp)    │  8 tools, 5 ui:// cards
                                                  │  dks/state.py  learning   │  SQLite | DynamoDB
                                                  │  dks/grader.py  Bedrock   │  Claude Sonnet 4.6 grader
                                                  └──────────────────────────┘
```

* The **browser is the MCP client**: `initialize` -> `notifications/initialized` -> `tools/list` ->
  `tools/call` / `resources/read`, with `Mcp-Session-Id` and `MCP-Protocol-Version` headers. Every message
  is shown in the right-hand log panel.
* The simulator backend never talks to the MCP server - it only gets the tool list and conversation from the
  browser and returns the LLM's decision. This mirrors how Alexa+ sits between the user and an add-on.
* Learner identity comes from `Authorization: Bearer <token>` (stand-in for Alexa account linking).

### Tools
`start_daily_scene`, `submit_learner_turn`, `get_review_queue`, `submit_review_answer`, `get_progress`,
`browse_scene_packs`, `purchase_scene_pack`, `schedule_practice`.

## Run locally

Requirements: Python 3.12, AWS credentials with Bedrock access to Claude Haiku 4.5 and Sonnet 4.6
(us-east-1 / us-west-2), Chrome for voice input.

```bash
pip install -r requirements.txt
cp .env.example .env          # set DKS_AWS_PROFILE if you use a named profile
python run_local.py           # MCP server :8790, simulator :8791
```

Open http://127.0.0.1:8791/ and say or type:

1. "Let's practice Korean" - today's scene starts; the characters speak.
2. Press **Speak Korean** (or type Korean) to answer. Try `아이스 아메리카노 한 잔 줘` to see a politeness fix.
3. Finish the scene, then **Next day** (demo clock) and "Let's review yesterday's phrases".
4. "How am I doing?", "What scene packs can I get?", "Buy the Office Life pack" -> "Yes".
5. "Remind me to practice every weekday at 7:30 in the morning".

Switch **Inline / Fullscreen / Voice-only** under the device to see the three display modes.

### Tests
```bash
python tests/smoke_mcp.py                 # whole flow over raw JSON-RPC + official SDK client check
pip install playwright && playwright install chromium
python tests/e2e_screenshots.py           # drives the simulator, writes screenshots/
```

### Content
Scenes are JSON files (`content/scenes/<scene_id>.json`) with lines for characters, the narrator and the
learner (accepted answers, hints, review targets). Audio is optional (`content/audio/<scene_id>/<line_id>.mp3`);
lines without approved audio are read by the browser's text-to-speech. `dks/catalog.json` decides which scenes
are free and which belong to packs. Sample scenes for development live in `fixtures/scenes/`.

## Deploy (AWS Lambda)
`deploy/template.yaml` (SAM) runs both apps on Lambda Function URLs (arm64, AWS Lambda Web Adapter) with a
DynamoDB table; the MCP server runs stateless with JSON responses there. `deploy/deploy.sh` builds and
deploys in two passes (the second sets the public MCP URL used for audio links and card CSP). It creates
billable resources - run it deliberately.

## Notes
* Bedrock request-per-minute quotas are per region and inference profile; model settings accept several
  `region|modelId` endpoints and rotate between them.
* "Alexa" and Echo are trademarks of Amazon. This project is an independent hackathon entry; the simulator
  uses its own visual design and no Amazon logos.

License: MIT.
