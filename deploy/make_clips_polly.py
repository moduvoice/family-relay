"""Make the demo family's voices with Amazon Polly: one fixed English Polly voice per family member, engine FR_POLLY_ENGINE (standard default; never the
assistant's voice), 16 kHz mono WAV per scenario step; the noisy step is the same voice mixed with TV-like noise
at about 0 dB SNR.

    python deploy/make_clips_polly.py [--region us-east-1]
writes simulator/static/clips/sNN-<who>.wav + manifest.json and prints each clip's length (requests must be >= 1.5 s).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import boto3
import numpy as np

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
from relay.voice.audio import wav_bytes  # noqa: E402

STATIC = APP / "simulator" / "static"
ENGINE = os.environ.get("FR_POLLY_ENGINE", "standard")   # standard | neural | generative


def synth(polly, text: str, lang: str, v: dict) -> np.ndarray:
    body = f"<prosody rate='{v['rate']}'>{text}</prosody>" if v.get("rate") else text
    r = polly.synthesize_speech(Text=f"<speak>{body}</speak>", TextType="ssml", VoiceId=v["VoiceId"], Engine=ENGINE,
                                OutputFormat="pcm", SampleRate="16000", LanguageCode=lang)
    x = np.frombuffer(r["AudioStream"].read(), np.int16).astype(np.float32) / 32768
    pad = np.zeros(4000, np.float32)                       # 0.25 s of room tone either side
    return np.concatenate([pad, x, pad])


def tv_noise(n: int, rnd) -> np.ndarray:
    w = rnd.standard_normal(n).astype(np.float32)
    babble = np.sin(np.linspace(0, n / 16000 * 2 * np.pi * 3, n)) * rnd.standard_normal(n) * 0.5
    z = (0.6 * w + babble).astype(np.float32)
    return z / (np.abs(z).max() + 1e-9)


def main(region: str) -> None:
    sc = json.loads((STATIC / "scenario.json").read_text(encoding="utf-8"))
    polly = boto3.client("polly", region_name=region)
    out = STATIC / "clips"
    out.mkdir(exist_ok=True)
    for old in out.glob("*.wav"):
        old.unlink()
    rnd = np.random.default_rng(7)
    manifest = {"steps": {}, "sample_rate": 16000, "source": f"Amazon Polly ({ENGINE})",
                "voices": {k: v["polly"] for k, v in sc["family"].items()}}
    short = []
    for i, s in enumerate(sc["steps"]):
        who = s["who"]
        x = synth(polly, s["text"], s.get("lang") or sc["family"][who]["lang"], sc["family"][who]["polly"])
        if s.get("clarity") == "noisy":
            z = tv_noise(len(x), rnd)
            x = x + z * (np.sqrt((x ** 2).mean()) / (np.sqrt((z ** 2).mean()) + 1e-9))
            x = x / (np.abs(x).max() + 1e-9) * 0.9
        name = f"s{i:02d}-{who.lower()}.wav"
        (out / name).write_bytes(wav_bytes(x, 16000))
        manifest["steps"][str(i)] = name
        sec = round(len(x) / 16000, 2)
        if sec < 1.5:
            short.append((i, sec))
        print(f"{name} {sec:5.2f}s {'overheard ' if s.get('overheard') else ''}{s['text']}", flush=True)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print("SHORT(<1.5s):", short or "none")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us-east-1")
    main(ap.parse_args().region)
