"""Runs on the GPU box (VoxCPM2 server up on :8000, Qwen3-ASR in /nvme/voxcpm2/models). Produces:
  - one consistent voice per family member (base VoxCPM2 + a voice description for the first line, then that
    take as the reference for the rest) speaking every scenario line -> clips/sNN-<who>.wav (16 kHz mono)
  - the noisy scenario step mixed with TV-like noise at ~0 dB SNR
  - the assistant's reference voice (assistant_ref.wav/.txt) for realtime TTS
  - bench.json: TTS latency per sentence (realtime check) and Qwen3-ASR transcripts + latency on every clip

    python make_clips.py scenario.json OUT_DIR
"""
from __future__ import annotations

import base64
import difflib
import io
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio
from transformers import AutoModelForMultimodalLM, AutoProcessor

URL = "http://127.0.0.1:8000/v1/audio/speech"
R = Path("/nvme/voxcpm2")
SEEDS = [42, 7, 1234]
scenario = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
OUT = Path(sys.argv[2])
(OUT / "clips").mkdir(parents=True, exist_ok=True)

proc = AutoProcessor.from_pretrained(str(R / "models/Qwen3-ASR-1.7B-hf"))
asr_model = AutoModelForMultimodalLM.from_pretrained(str(R / "models/Qwen3-ASR-1.7B-hf"), device_map="cuda",
                                                     dtype=torch.bfloat16).eval()
ASR_LANG = {"en": "English", "ko": "Korean"}


def tts(text, lang, desc=None, ref=None, seed=42):
    body = {"model": "voxcpm2", "response_format": "wav", "stream": False, "language": lang, "seed": seed,
            "input": f"({desc}){text}" if desc and not ref else text}
    if ref:
        body["ref_audio"], body["ref_text"] = ref
    t0 = time.time()
    req = urllib.request.Request(URL, json.dumps(body, ensure_ascii=False).encode(), {"Content-Type": "application/json"})
    data = urllib.request.urlopen(req, timeout=300).read()
    ms = int((time.time() - t0) * 1000)
    x, sr = sf.read(io.BytesIO(data), dtype="float32")
    x = x.mean(1) if x.ndim > 1 else x
    return x, sr, ms, data


def to16k(x, sr):
    return torchaudio.functional.resample(torch.from_numpy(x), sr, 16000).numpy()


def asr(x16, lang):
    t0 = time.time()
    inp = proc.apply_transcription_request(audio=x16, language=ASR_LANG.get(lang)).to(asr_model.device, asr_model.dtype)
    with torch.no_grad():
        o = asr_model.generate(**inp, max_new_tokens=128, do_sample=False)
    text = proc.decode(o[:, inp["input_ids"].shape[1]:], return_format="transcription_only")[0]
    return text.strip(), int((time.time() - t0) * 1000)


def norm(t):
    return re.sub(r"[^0-9a-z가-힣]", "", (t or "").lower())


def sim(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def best_take(text, lang, desc=None, ref=None):
    """Up to 3 seeds; keep the first whose ASR matches the text closely (>= 0.9), else the closest."""
    takes = []
    for seed in SEEDS:
        x, sr, ms, raw = tts(text, lang, desc, ref, seed)
        x16 = to16k(x, sr)
        heard, asr_ms = asr(x16, lang)
        takes.append({"x": x, "sr": sr, "x16": x16, "ms": ms, "heard": heard, "sim": sim(text, heard), "seed": seed,
                      "raw": raw, "audio_sec": round(len(x) / sr, 2)})
        if takes[-1]["sim"] >= 0.9:
            break
    return max(takes, key=lambda t: t["sim"]), len(takes)


def data_uri(raw):
    return "data:audio/wav;base64," + base64.b64encode(raw).decode()


def tv_noise(n, rnd):
    w = rnd.standard_normal(n).astype(np.float32)
    b = np.cumsum(w)                       # brown-ish rumble
    b = (b - np.convolve(b, np.ones(400) / 400, mode="same"))
    babble = np.sin(np.linspace(0, n / 16000 * 2 * np.pi * 3, n)) * rnd.standard_normal(n) * 0.5
    z = (0.6 * w + 0.03 * b + babble).astype(np.float32)
    return z / (np.abs(z).max() + 1e-9)


bench = {"tts": [], "family": {}, "clips": []}
anchors = {}
manifest = {"steps": {}, "sample_rate": 16000}
rnd = np.random.default_rng(7)
for i, s in enumerate(scenario["steps"]):
    who = s["who"]
    lang = (s.get("lang") or scenario["family"][who]["lang"]).split("-")[0]
    desc = scenario["family"][who]["tts_desc"]
    if who not in anchors:
        t, tries = best_take(s["text"], lang, desc=desc)
        anchors[who] = (data_uri(t["raw"]), s["text"])
        bench["family"][who] = {"anchor_step": i, "anchor_tries": tries, "anchor_sim": round(t["sim"], 3)}
    else:
        t, tries = best_take(s["text"], lang, ref=anchors[who])
    x16 = t["x16"]
    if s.get("clarity") == "noisy":
        z = tv_noise(len(x16), rnd)
        x16 = x16 + z * (np.sqrt((x16 ** 2).mean()) / (np.sqrt((z ** 2).mean()) + 1e-9))   # ~0 dB SNR
        x16 = x16 / (np.abs(x16).max() + 1e-9) * 0.9
    name = f"s{i:02d}-{who.lower()}.wav"
    sf.write(OUT / "clips" / name, x16, 16000, subtype="PCM_16")
    manifest["steps"][str(i)] = name
    heard, asr_ms = asr(x16, lang)
    bench["clips"].append({"step": i, "who": who, "text": s["text"], "tries": tries, "seed": t["seed"],
                           "tts_ms": t["ms"], "audio_sec": t["audio_sec"], "qwen3asr_text": heard,
                           "qwen3asr_ms": asr_ms, "qwen3asr_sim": round(sim(s["text"], heard), 3),
                           "noisy": s.get("clarity") == "noisy"})
    print(json.dumps(bench["clips"][-1], ensure_ascii=False), flush=True)

# assistant voice: one reference take, then realtime-style replies with that reference
a_text = "Hi! I'm your family's assistant. I'll pass messages along when I hear the right voice."
t, tries = best_take(a_text, "en", desc="A warm, friendly adult female voice assistant with a clear American English accent, natural and calm")
(OUT / "assistant_ref.wav").write_bytes(t["raw"])
(OUT / "assistant_ref.txt").write_text(a_text, encoding="utf-8")
ref = (data_uri(t["raw"]), a_text)
replies = [("It's 6:05 PM. By the way, your dad asked me to tell you to eat dinner tonight - there's kimchi stew in the fridge.", "en"),
           ("I'll let your dad know.", "en"),
           ("Yes - she said it was delicious, and she heard your message at 6:05.", "en"),
           ("Good morning! It's 7:00 AM.", "en"),
           ("지금은 낮 12시 20분이에요. 그런데 엄마가 오후 3시에 병원 예약이 있다고 전해 달래요.", "ko"),
           ("There are three live items in the family memory.", "en")]
for text, lang in replies:
    for k in range(2):  # second call shows the warm latency
        x, sr, ms, raw = tts(text, lang, ref=ref)
        bench["tts"].append({"text": text, "lang": lang, "run": k, "ms": ms, "audio_sec": round(len(x) / sr, 2),
                             "rtf": round(ms / 1000 / (len(x) / sr), 3)})
        print(json.dumps(bench["tts"][-1], ensure_ascii=False), flush=True)
(OUT / "clips" / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
(OUT / "bench.json").write_text(json.dumps(bench, ensure_ascii=False, indent=1), encoding="utf-8")
print("DONE", OUT)
