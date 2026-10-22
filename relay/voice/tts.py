"""Text-to-speech. Interface: synthesize(text: str, lang: str) -> dict {"audio": bytes, "mime": str, "engine": str, "ms": int}

Engines (FR_TTS_ENGINE, default from FR_PROFILE):
  polly    Amazon Polly (profile aws) - Standard engine by default (FR_POLLY_ENGINE), assistant voice Salli (family
           demo voices are Matthew, Joanna, Ivy, Kimberly), Seoyeon for Korean
  piper    on-device Piper voice through sherpa-onnx (profile local); English only, other languages fall back to
           the browser voice (the client logs it)
  voxcpm2  our VoxCPM2 server on the GPU voice box (profile gpu), one fixed reference clip keeps the voice constant
  browser  the browser's Web Speech API (fallback)
"""
from __future__ import annotations

import base64
import json
import time
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from .. import config
from .audio import wav_bytes


class TtsUnsupported(Exception):
    """This engine cannot speak this language; the client falls back to the browser voice."""


class TtsEngine(Protocol):
    name: str

    def synthesize(self, text: str, lang: str) -> dict[str, Any]: ...


class PollyTts:
    name = "amazon-polly"

    def __init__(self, region: str, voice_en: str, engine_en: str, voice_ko: str):
        import boto3

        self.client = boto3.client("polly", region_name=region)
        self.voices = {"en": (voice_en, engine_en), "ko": (voice_ko, engine_en)}

    def synthesize(self, text, lang):
        code = (lang or "en-US").split("-")[0]
        voice, engine = self.voices.get(code, self.voices["en"])
        t0 = time.time()
        r = self.client.synthesize_speech(Text=text, VoiceId=voice, Engine=engine, OutputFormat="mp3")
        return {"audio": r["AudioStream"].read(), "mime": "audio/mpeg", "engine": f"polly:{voice}/{engine}",
                "ms": int((time.time() - t0) * 1000)}


class PiperTts:
    name = "piper"

    def __init__(self, models: Path, voice_dir: str, threads: int):
        import sherpa_onnx as so

        d = models / voice_dir
        model = next(d.glob("*.onnx"))
        mc = so.OfflineTtsModelConfig(num_threads=threads, provider="cpu")
        mc.vits = so.OfflineTtsVitsModelConfig(model=str(model), lexicon="", tokens=str(d / "tokens.txt"),
                                               data_dir=str(d / "espeak-ng-data"))
        self.tts = so.OfflineTts(so.OfflineTtsConfig(model=mc, max_num_sentences=1))
        self.voice = voice_dir

    def synthesize(self, text, lang):
        if (lang or "en-US").split("-")[0] != "en":
            raise TtsUnsupported(f"{self.voice} speaks English only")
        t0 = time.time()
        a = self.tts.generate(text, sid=0, speed=1.0)
        return {"audio": wav_bytes(a.samples, a.sample_rate), "mime": "audio/wav", "engine": f"piper:{self.voice}",
                "ms": int((time.time() - t0) * 1000)}


class VoxCpm2Tts:
    name = "voxcpm2"

    def __init__(self, url: str, ref_wav: str, ref_text: str, desc: str):
        self.url = url
        self.ref = None
        if ref_wav and Path(ref_wav).exists():
            self.ref = ("data:audio/wav;base64," + base64.b64encode(Path(ref_wav).read_bytes()).decode(), ref_text)
        self.desc = desc

    def synthesize(self, text, lang):
        body: dict[str, Any] = {"model": "voxcpm2", "response_format": "wav", "stream": False, "seed": 42,
                                "language": {"ko": "ko", "zh": "zh"}.get((lang or "en").split("-")[0], "en")}
        if self.ref:
            body["ref_audio"], body["ref_text"] = self.ref
            body["input"] = text
        else:
            body["input"] = f"({self.desc}){text}" if self.desc else text
        t0 = time.time()
        req = urllib.request.Request(self.url, data=json.dumps(body, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            audio = r.read()
        return {"audio": audio, "mime": "audio/wav", "engine": "voxcpm2", "ms": int((time.time() - t0) * 1000)}


_engine: TtsEngine | None = None


def get_tts() -> TtsEngine | None:
    """None means the browser speaks (Web Speech fallback)."""
    global _engine
    eng = config.TTS_ENGINE
    if eng == "browser":
        return None
    if _engine is None:
        if eng == "polly":
            _engine = PollyTts(config.env("FR_POLLY_REGION", "us-east-1"), config.env("FR_POLLY_VOICE", "Salli"),
                               config.env("FR_POLLY_ENGINE", "standard"), config.env("FR_POLLY_VOICE_KO", "Seoyeon"))
        elif eng == "piper":
            _engine = PiperTts(Path(config.env("FR_LOCAL_MODELS", str(config.APP_DIR / "models"))),
                               config.env("FR_PIPER_VOICE", "vits-piper-en_US-amy-low"), int(config.env("FR_LOCAL_THREADS", "2")))
        elif eng == "voxcpm2":
            _engine = VoxCpm2Tts(config.env("FR_TTS_URL", "http://127.0.0.1:8000/v1/audio/speech"),
                                 config.env("FR_TTS_REF_WAV", str(config.APP_DIR / "assets" / "voice" / "assistant_ref.wav")),
                                 config.env("FR_TTS_REF_TEXT", "Hi! I'm your family's assistant. I'll pass messages along "
                                            "when I hear the right voice."),
                                 config.env("FR_TTS_VOICE_DESC", "A warm, friendly adult female voice assistant with a "
                                            "clear American English accent, natural and calm"))
        else:
            raise ValueError(f"unknown FR_TTS_ENGINE={eng!r}")
    return _engine
