"""Speech-to-text. Interface: transcribe(audio: bytes, mime: str, lang_hint: str | None, context: str = "") -> dict
    {"text": str, "lang": str | None, "engine": str, "ms": int, ...}
context: words likely to be said (the family's names) - used as a vocabulary hint where the engine supports it.

Engines (FR_STT_ENGINE, default from FR_PROFILE):
  transcribe  Amazon Transcribe streaming (profile aws)
  sherpa      on-device: silero VAD + sherpa-onnx parakeet-110M int8 for English, SenseVoice int8 for other
              languages (profile local)
  http        our STT server, Qwen3-ASR-1.7B on the GPU voice box (profile gpu)
  whisper     faster-whisper on this machine
  browser     the browser's Web Speech API does it (fallback)
"""
from __future__ import annotations

import asyncio
import io
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from .. import config
from .audio import SR, pcm16_bytes, to_pcm16k


class SttEngine(Protocol):
    name: str

    def transcribe(self, audio: bytes, mime: str, lang_hint: str | None, context: str = "") -> dict[str, Any]: ...


class TranscribeStt:
    """Amazon Transcribe streaming (amazon-transcribe SDK): short utterances, no S3 round trip.
    vocabulary: an optional Transcribe custom vocabulary with how the family's names are spelled (a name list,
    not a voice enrollment) - general ASR spells uncommon names many ways ("Sunhee" -> "Sunhi", "Soon he")."""

    name = "amazon-transcribe"

    def __init__(self, region: str, vocabulary: str = ""):
        self.region, self.vocabulary = region, vocabulary
        self.timeout_s = float(config.env("FR_TRANSCRIBE_TIMEOUT_S", "15"))

    async def _run(self, pcm: bytes, lang: str) -> str:
        from amazon_transcribe.client import TranscribeStreamingClient
        from amazon_transcribe.handlers import TranscriptResultStreamHandler

        client = TranscribeStreamingClient(region=self.region)
        kw = {"vocabulary_name": self.vocabulary} if self.vocabulary and lang.startswith("en") else {}
        stream = await client.start_stream_transcription(language_code=lang, media_sample_rate_hz=SR,
                                                         media_encoding="pcm", **kw)
        parts: list[str] = []

        class H(TranscriptResultStreamHandler):
            async def handle_transcript_event(self, event):
                for r in event.transcript.results:
                    if not r.is_partial and r.alternatives:
                        parts.append(r.alternatives[0].transcript)

        async def feed():
            for i in range(0, len(pcm), 8000):           # 250 ms chunks
                await stream.input_stream.send_audio_event(audio_chunk=pcm[i:i + 8000])
            await stream.input_stream.end_stream()

        await asyncio.gather(feed(), H(stream.output_stream).handle_events())
        return " ".join(parts).strip()

    def transcribe(self, audio, mime, lang_hint, context=""):
        t0 = time.time()
        x = to_pcm16k(audio)
        lang = lang_hint if lang_hint and "-" in lang_hint else "en-US"
        # a streaming session can stall without an answer (seen once in a 30-utterance run): bound each attempt
        # and open a new session once before giving up
        for attempt in range(2):
            try:
                text = asyncio.run(asyncio.wait_for(self._run(pcm16_bytes(x), lang), timeout=self.timeout_s))
                break
            except asyncio.TimeoutError:
                if attempt == 1:
                    raise RuntimeError(f"Amazon Transcribe gave no result within {self.timeout_s:.0f} s (2 sessions)") from None
        return {"text": text, "lang": lang, "engine": self.name, "ms": int((time.time() - t0) * 1000),
                "audio_sec": round(len(x) / SR, 2)}


class SherpaStt:
    """On-device STT with sherpa-onnx (CPU): silero VAD trims the utterance, parakeet-110M CTC int8 transcribes
    English, SenseVoice int8 the other languages (parakeet is English-only)."""

    name = "sherpa-onnx"

    def __init__(self, models: Path, threads: int):
        import sherpa_onnx as so

        self.so, self.threads = so, threads
        self.models = models
        d = models / "sherpa-onnx-nemo-parakeet_tdt_ctc_110m-en-36000-int8"
        self.en = so.OfflineRecognizer.from_nemo_ctc(model=str(d / "model.int8.onnx"), tokens=str(d / "tokens.txt"),
                                                     num_threads=threads)
        self.other = None
        vc = so.VadModelConfig()
        vc.silero_vad.model = str(models / "silero_vad.onnx")
        vc.silero_vad.min_silence_duration = 0.25
        vc.sample_rate, vc.num_threads = SR, 1
        self.vad_cfg = vc

    def _other(self):
        if self.other is None:
            d = self.models / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
            self.other = self.so.OfflineRecognizer.from_sense_voice(model=str(d / "model.int8.onnx"),
                                                                    tokens=str(d / "tokens.txt"), num_threads=self.threads,
                                                                    use_itn=True, language="auto")
        return self.other

    def vad_trim(self, x):
        import numpy as np

        vad = self.so.VoiceActivityDetector(self.vad_cfg, buffer_size_in_seconds=60)
        segs = []
        w = np.concatenate([x, np.zeros(SR // 2, np.float32)])
        for i in range(0, len(w) - 512, 512):
            vad.accept_waveform(w[i:i + 512])
            while not vad.empty():
                segs.append(np.array(vad.front.samples, np.float32))
                vad.pop()
        vad.flush()
        while not vad.empty():
            segs.append(np.array(vad.front.samples, np.float32))
            vad.pop()
        return np.concatenate(segs) if segs else x

    def transcribe(self, audio, mime, lang_hint, context=""):
        t0 = time.time()
        x = to_pcm16k(audio)
        t1 = time.time()
        seg = self.vad_trim(x)
        t2 = time.time()
        lang = (lang_hint or "en-US").split("-")[0]
        rec = self.en if lang == "en" else self._other()
        s = rec.create_stream()
        s.accept_waveform(SR, seg)
        rec.decode_stream(s)
        text = s.result.text.strip()
        return {"text": text, "lang": lang_hint, "engine": f"{self.name}:{'parakeet-110m-int8' if lang == 'en' else 'sensevoice-int8'}",
                "ms": int((time.time() - t0) * 1000), "vad_ms": int((t2 - t1) * 1000), "decode_ms": int((time.time() - t2) * 1000),
                "audio_sec": round(len(x) / SR, 2), "speech_sec": round(len(seg) / SR, 2)}


class WhisperStt:
    """faster-whisper (CTranslate2). Open weights; runs on CPU (int8) or GPU."""

    name = "whisper"

    def __init__(self, model: str, device: str, compute_type: str):
        from faster_whisper import WhisperModel

        self.model_name = model
        self.model = WhisperModel(model, device=device, compute_type=compute_type,
                                  download_root=config.env("FR_STT_CACHE") or None)

    def transcribe(self, audio, mime, lang_hint, context=""):
        t0 = time.time()
        lang = (lang_hint or "").split("-")[0] or None
        segments, info = self.model.transcribe(io.BytesIO(audio), language=lang, beam_size=1, vad_filter=True,
                                               initial_prompt=context or None, condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segments).strip()
        return {"text": text, "lang": info.language, "engine": f"whisper:{self.model_name}",
                "ms": int((time.time() - t0) * 1000), "audio_sec": round(info.duration, 2)}


class HttpStt:
    """Any STT server that takes raw audio and returns {"text", "lang"} (e.g. Qwen3-ASR on a GPU box)."""

    name = "http"

    def __init__(self, url: str):
        self.url = url

    def transcribe(self, audio, mime, lang_hint, context=""):
        t0 = time.time()
        req = urllib.request.Request(self.url, data=audio, headers={"Content-Type": mime or "application/octet-stream",
                                                                    "X-Lang": lang_hint or "",
                                                                    "X-Context": urllib.parse.quote(context or "")})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
        return {**d, "engine": d.get("engine", "http"), "server_ms": d.get("ms"), "ms": int((time.time() - t0) * 1000)}


_engine: SttEngine | None = None


def get_stt() -> SttEngine | None:
    """None means the browser does STT itself (Web Speech fallback)."""
    global _engine
    eng = config.STT_ENGINE
    if eng == "browser":
        return None
    if _engine is None:
        if eng == "transcribe":
            _engine = TranscribeStt(config.env("FR_TRANSCRIBE_REGION", "us-east-1"), config.env("FR_TRANSCRIBE_VOCABULARY"))
        elif eng == "sherpa":
            _engine = SherpaStt(Path(config.env("FR_LOCAL_MODELS", str(config.APP_DIR / "models"))),
                                int(config.env("FR_LOCAL_THREADS", "2")))
        elif eng == "whisper":
            _engine = WhisperStt(config.env("FR_STT_MODEL", "large-v3-turbo"), config.env("FR_STT_DEVICE", "cpu"),
                                 config.env("FR_STT_COMPUTE", "int8"))
        elif eng == "http":
            _engine = HttpStt(config.env("FR_STT_URL", "http://127.0.0.1:8002/stt"))
        else:
            raise ValueError(f"unknown FR_STT_ENGINE={eng!r}")
    return _engine
