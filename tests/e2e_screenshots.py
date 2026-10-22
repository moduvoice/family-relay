"""Play the 4-person family scenario in the simulator (headless Chromium), save screenshots per step and,
with --video, a screen recording of the whole run plus one clip per scene of the demo video.

Uses the real MCP server, the configured speaker module and voice/LLM profile.

    python tests/e2e_screenshots.py [--base http://127.0.0.1:8791] [--out screenshots] [--video] [--size 1920x1080]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

SHOTS = {  # scenario step index -> screenshot name
    3: "01-voices-forming", 11: "02-overheard-name-cue", 18: "03-dad-leaves-message", 19: "04-mom-leaves-message",
    22: "05-noisy-voice-silent", 23: "06-grandma-gets-message", 24: "07-hannah-gets-message", 25: "08-hannah-replies",
    26: "09-dad-asks-did-she-eat", 27: "10-family-memory", 28: "11-voice-clusters", 29: "12-faded-after-24h",
}
SCENES = {  # demo-video scene clip -> (first step, last step)
    "scene2-voices-forming": (0, 17),
    "scene3-dad-leaves-message": (18, 19),
    "scene4-noise-dropped-grandma-gets-message": (22, 23),
    "scene5-hannah-gets-message": (24, 24),
    "scene6-hannah-replies": (25, 25),
    "scene7-did-she-eat-family-memory": (26, 27),
    "scene7b-voices-and-24h-fade": (28, 29),
}


def main(base: str, out: Path, video: bool, size: tuple[int, int]) -> int:
    out.mkdir(parents=True, exist_ok=True)
    steps = []
    w, h = size
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        vdir = out / "video-raw"
        ctx = browser.new_context(viewport={"width": w, "height": h},
                                  **({"record_video_dir": str(vdir), "record_video_size": {"width": w, "height": h}} if video else {}))
        page = ctx.new_page()
        v0 = time.time()                      # the recording starts with the page
        errors: list[str] = []
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.goto(base + "/")
        page.wait_for_selector("#conn.ok", timeout=20000)
        page.uncheck("#sound")
        page.evaluate("async () => { await FRSim.demoClock({reset: true}); FRSim.resetScreen(); FRSim.state.messages = []; }")
        n = page.evaluate("FRSim.SCENARIO.length")
        page.screenshot(path=str(out / "00-home.png"))
        for i in range(n):
            llm_before = len(page.query_selector_all("#log-list .entry.llm"))
            err_before = len(page.query_selector_all("#log-list .entry.err"))
            spk_before = len(page.query_selector_all("#log-list .entry.spk"))
            t0 = time.time()
            page.evaluate(f"FRSim.runStep({i})", )
            page.wait_for_function("() => !FRSim.state.busy && FRSim.state.queue.length === 0", timeout=180000)
            time.sleep(1.6)
            s = page.evaluate(f"FRSim.SCENARIO[{i}]")
            rec = {"step": i, "who": s["who"], "said": s["text"], "seconds": round(time.time() - t0, 1),
                   "video_from_s": round(t0 - v0, 2), "video_to_s": round(time.time() - v0, 2),
                   "speaker": page.inner_text("#spk-name") + " / " + page.inner_text("#spk-state"),
                   "decision": page.inner_text("#spk-decision"),
                   "llm": page.eval_on_selector_all("#log-list .entry.llm .what", "e => e.map(x => x.textContent)")[llm_before:],
                   "errors": page.eval_on_selector_all("#log-list .entry.err .line1", "e => e.map(x => x.textContent)")[err_before:],
                   "heard": page.inner_text("#cap-you"),
                   "voice": [t for t in page.eval_on_selector_all("#log-list .entry.spk .line1", "e => e.map(x => x.textContent)")[spk_before:]
                             if "STT" in t or "TTS" in t],
                   "assistant": page.inner_text("#cap-assistant")}
            if i in SHOTS:
                page.screenshot(path=str(out / f"{SHOTS[i]}.png"))
                page.locator("#screen").screenshot(path=str(out / f"{SHOTS[i]}-device.png"))
                rec["screenshot"] = SHOTS[i]
            steps.append(rec)
            print(json.dumps(rec, ensure_ascii=False), flush=True)
        page.click("#mode-seg button[data-mode=fullscreen]")
        time.sleep(1.2)
        page.locator("#screen").screenshot(path=str(out / "13-fullscreen-card.png"))
        (out / "e2e_run.json").write_text(json.dumps({"steps": steps, "console_errors": errors}, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
        ctx.close()   # finishes the video file
        browser.close()
        if video:     # webm -> mp4, then one clip per demo-video scene
            raw = next(vdir.glob("*.webm"))
            full = out / "simulator-run.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
                            str(full)], check=True)
            shutil.rmtree(vdir)
            for name, (a, b) in SCENES.items():
                if b >= len(steps):
                    continue
                start, end = max(0.0, steps[a]["video_from_s"] - 0.5), steps[b]["video_to_s"] + 1.0
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", str(full),
                                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", str(out / f"{name}.mp4")], check=True)
    print("console errors:", errors)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8791")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "screenshots"))
    ap.add_argument("--video", action="store_true", help="record simulator-run.mp4 and per-scene clips (needs ffmpeg)")
    ap.add_argument("--size", default="1920x1080", help="browser viewport / video size, e.g. 1920x1080")
    a = ap.parse_args()
    sw, sh = (int(x) for x in a.size.split("x"))
    raise SystemExit(main(a.base, Path(a.out), a.video, (sw, sh)))
