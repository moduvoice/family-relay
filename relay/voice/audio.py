"""Audio helpers shared by the voice engines: any input (WAV, webm/opus from a browser recorder) -> 16 kHz mono."""
from __future__ import annotations

import io
import subprocess
import wave

import numpy as np

SR = 16000


def to_pcm16k(audio: bytes) -> np.ndarray:
    """float32 mono 16 kHz. 16-bit WAV at 16 kHz is read directly; everything else goes through ffmpeg."""
    if audio[:4] == b"RIFF":
        with wave.open(io.BytesIO(audio)) as w:
            if w.getframerate() == SR and w.getsampwidth() == 2:
                x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
                return x.reshape(-1, w.getnchannels()).mean(1)
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"],
                       input=audio, capture_output=True, check=True)
    return np.frombuffer(p.stdout, np.int16).astype(np.float32) / 32768


def pcm16_bytes(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


def wav_bytes(x: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16_bytes(np.asarray(x, np.float32)))
    return buf.getvalue()
