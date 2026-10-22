"""Compare STT engines on the recorded scenario clips (simulator/static/clips) through the app's STT module.

    FR_STT_ENGINE=whisper FR_STT_MODEL=large-v3-turbo python tests/stt_bench.py
    FR_STT_ENGINE=http FR_STT_URL=http://127.0.0.1:8002/stt python tests/stt_bench.py
"""
from __future__ import annotations

import difflib
import json
import re
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
from relay.voice.stt import get_stt  # noqa: E402

STATIC = APP / "simulator" / "static"


def norm(t: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", (t or "").lower())


def main() -> None:
    sc = json.loads((STATIC / "scenario.json").read_text(encoding="utf-8"))
    man = json.loads((STATIC / "clips" / "manifest.json").read_text(encoding="utf-8"))
    t0 = time.time()
    eng = get_stt()
    load_s = round(time.time() - t0, 1)
    rows = []
    for i, s in enumerate(sc["steps"]):
        audio = (STATIC / "clips" / man["steps"][str(i)]).read_bytes()
        lang = s.get("lang") or sc["family"][s["who"]]["lang"]
        r = eng.transcribe(audio, "audio/wav", lang)
        sim = difflib.SequenceMatcher(None, norm(s["text"]), norm(r["text"])).ratio()
        rows.append({"step": i, "lang": lang, "noisy": s.get("clarity") == "noisy", "ms": r["ms"], "sim": round(sim, 3),
                     "ref": s["text"], "hyp": r["text"]})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    ok = sum(1 for r in rows if r["sim"] >= 0.95)
    ms = sorted(r["ms"] for r in rows)
    print(json.dumps({"engine": eng.name if eng else "browser", "model_load_s": load_s, "clips": len(rows),
                      "near_exact": ok, "mean_sim": round(sum(r["sim"] for r in rows) / len(rows), 3),
                      "ms_median": ms[len(ms) // 2], "ms_max": ms[-1]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
