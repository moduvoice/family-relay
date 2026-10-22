"""Qwen3-ASR-1.7B speech-to-text server for the GPU box (the app's FR_STT_ENGINE=http target).

POST /stt with raw audio (wav, or webm/ogg from a browser recorder - decoded with ffmpeg), header X-Lang
(e.g. en-US, ko-KR; empty = auto). Returns {"text", "lang", "engine", "ms", "audio_sec"}.

    /nvme/voxcpm2/venv-asr/bin/python stt_server.py [--port 8002]
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

import numpy as np
import soundfile as sf
import torch
from transformers import AutoModelForMultimodalLM, AutoProcessor

MODEL = Path("/nvme/voxcpm2/models/Qwen3-ASR-1.7B-hf")
LANGS = {"en": "English", "ko": "Korean", "es": "Spanish", "zh": "Chinese", "ja": "Japanese"}
proc = AutoProcessor.from_pretrained(str(MODEL))
model = AutoModelForMultimodalLM.from_pretrained(str(MODEL), device_map="cuda", dtype=torch.bfloat16).eval()
GPU = Lock()


def decode(audio: bytes, mime: str) -> np.ndarray:
    if "wav" in mime or audio[:4] == b"RIFF":
        x, sr = sf.read(io.BytesIO(audio), dtype="float32")
        x = x.mean(1) if x.ndim > 1 else x
        if sr != 16000:
            import torchaudio
            x = torchaudio.functional.resample(torch.from_numpy(x), sr, 16000).numpy()
        return x
    out = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"],
                         input=audio, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.rstrip("/") != "/stt":
            self.send_error(404)
            return
        t0 = time.time()
        audio = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        lang = (self.headers.get("X-Lang") or "").split("-")[0]
        context = urllib.parse.unquote(self.headers.get("X-Context") or "")
        try:
            x = decode(audio, self.headers.get("Content-Type", ""))
            with GPU:
                inp = proc.apply_transcription_request(audio=x, language=LANGS.get(lang),
                                                       prompt=f"Vocabulary: {context}" if context else None).to(model.device, model.dtype)
                with torch.no_grad():
                    o = model.generate(**inp, max_new_tokens=160, do_sample=False)
            parsed = proc.decode(o[:, inp["input_ids"].shape[1]:], return_format="parsed")[0]
            body = {"text": (parsed.get("transcription") or "").strip(), "lang": parsed.get("language") or lang,
                    "engine": "qwen3-asr-1.7b (gpu)", "ms": int((time.time() - t0) * 1000), "audio_sec": round(len(x) / 16000, 2)}
            code = 200
        except Exception as e:
            body, code = {"error": f"{type(e).__name__}: {e}"}, 500
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8002)
    a = ap.parse_args()
    print(f"qwen3-asr stt on 127.0.0.1:{a.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
