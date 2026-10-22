"""Drive the simulator end to end in headless Chromium (typed input instead of the microphone) and save screenshots.

Requires the servers from run_local.py. Uses the real orchestrator LLM, MCP server and grader.

    python tests/e2e_screenshots.py [--base http://127.0.0.1:8791] [--out screenshots]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
from dks.scenes import load_scenes  # noqa: E402


def main(base: str, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    scenes = load_scenes()
    steps: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 960}, device_scale_factor=1)
        errors: list[str] = []
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type in ("error",) else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.goto(base + "/")
        page.wait_for_selector("#conn.ok", timeout=20000)
        page.uncheck("#sound")
        page.click("#reset")
        time.sleep(1)

        def idle(timeout=120):
            page.wait_for_function("() => !window.DKSSim.state.busy && window.DKSSim.state.queue.length === 0", timeout=timeout * 1000)
            time.sleep(1.8)  # card render + carousel transitions

        def tools_since(n_before: int) -> list[str]:
            return page.eval_on_selector_all(
                "#log-list .entry.llm .what", "els => els.map(e => e.textContent)")[n_before:]

        def n_llm() -> int:
            return len(page.query_selector_all("#log-list .entry.llm"))

        def shot(name: str, device_only: bool = False):
            path = out / f"{name}.png"
            if device_only:
                page.locator("#screen").screenshot(path=str(path))
            else:
                page.screenshot(path=str(path))
            return path.name

        def turn(kind: str, text: str, name: str | None = None, device_only=False):
            before = n_llm()
            err_before = len(page.query_selector_all("#log-list .entry.err"))
            t0 = time.time()
            if kind == "chip":
                page.click(f"#chips button[data-say=\"{text}\"]")
            else:
                page.fill("#text", text)
                page.press("#text", "Enter")
            time.sleep(0.5)
            idle()
            rec = {"input": text, "llm_steps": tools_since(before), "seconds": round(time.time() - t0, 1),
                   "assistant": page.inner_text("#cap-assistant"),
                   "errors": page.eval_on_selector_all("#log-list .entry.err .line1", "els => els.map(e => e.textContent)")[err_before:]}
            if name:
                rec["screenshot"] = shot(name, device_only)
            steps.append(rec)
            print(json.dumps(rec, ensure_ascii=False))
            return rec

        steps.append({"screenshot": shot("01-home")})
        turn("chip", "Let's practice Korean", "02-scene-start")
        scene_id = page.evaluate("""() => { const e=[...document.querySelectorAll('#log-list .entry.mcp pre')].map(p=>p.textContent).reverse()
            .find(t=>t.includes('"scene_id"')); return e ? JSON.parse(e).result.structuredContent.scene.scene_id : null }""")
        scene = scenes[scene_id]
        learner_lines = [l for l in scene.lines if l["speaker"] == "learner"]
        # first turn: casual ending (wrong politeness) to show the grader and the review queue
        first = learner_lines[0]["expected"][0]
        casual = first.replace("주세요", "줘").replace("요.", ".").replace("요", "") if first.endswith(("요", "요.", "요?")) else first + " 줘"
        turn("type", casual, "03-feedback")
        shot("03b-feedback-device", device_only=True)
        last = None
        for i, line in enumerate(learner_lines[1:], start=2):
            last = turn("type", line["expected"][0], "04-next-turn" if i == 2 else None)
            if "complete" in (last["assistant"] or "").lower():
                pass
        shot("05-scene-complete")
        page.click("#mode-seg button[data-mode=fullscreen]")
        time.sleep(1.2)
        shot("06-fullscreen-card", device_only=True)
        page.click("#mode-seg button[data-mode=inline]")
        time.sleep(0.6)

        page.click("#next-day")
        time.sleep(1.2)
        steps.append({"screenshot": shot("07-next-day")})
        turn("chip", "Let's review yesterday's phrases", "08-review-card")
        turn("type", learner_lines[0]["expected"][0], "09-review-graded")
        turn("chip", "How am I doing?", "10-progress-carousel")
        turn("chip", "What scene packs can I get?", "11-scene-packs")
        turn("type", "I'd like to buy the Office Life pack", "12-purchase-confirm")
        turn("type", "Yes, buy it", "13-purchased")
        turn("chip", "Remind me to practice every weekday at 7:30 in the morning", "14-schedule")
        page.click("#mode-seg button[data-mode=voice-only]")
        turn("chip", "How am I doing?", "15-voice-only", device_only=True)
        page.click("#mode-seg button[data-mode=inline]")

        (out / "e2e_run.json").write_text(json.dumps({"scene_id": scene_id, "steps": steps, "console_errors": errors},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()
    print("console errors:", errors)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8791")
    ap.add_argument("--out", default=str(APP / "screenshots"))
    a = ap.parse_args()
    raise SystemExit(main(a.base, Path(a.out)))
