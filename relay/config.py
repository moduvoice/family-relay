"""Settings read from the environment (and an optional .env file next to the app)."""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(APP_DIR / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name) or default


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if not value:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# FR_PROFILE picks every stage at once; any single FR_* / FV_* variable set explicitly still wins.
#   aws   (submission default) Amazon Transcribe -> Bedrock Nova 2 Lite -> Amazon Polly, speaker = familyvoice
#   local (on the device CPU)  silero VAD + sherpa-onnx parakeet-110M int8 -> cloud LLM -> Piper, familyvoice int8
#   gpu   (our GPU voice box)  Qwen3-ASR-1.7B -> cloud LLM -> VoxCPM2, familyvoice
_FV = "familyvoice.app_adapter:FamilyVoiceSpeaker"
PROFILES = {
    "aws": {"FR_STT_ENGINE": "transcribe", "FR_LLM_PROVIDER": "nova", "FR_TTS_ENGINE": "polly",
            "FR_SPEAKER_MODULE": _FV, "FV_MODEL": "r34", "FV_PROFILE": "server"},
    "local": {"FR_STT_ENGINE": "sherpa", "FR_LLM_PROVIDER": "nova", "FR_TTS_ENGINE": "piper",
              "FR_SPEAKER_MODULE": _FV, "FV_MODEL": "r34", "FV_PROFILE": "device"},
    "gpu": {"FR_STT_ENGINE": "http", "FR_LLM_PROVIDER": "nova", "FR_TTS_ENGINE": "voxcpm2",
            "FR_SPEAKER_MODULE": _FV, "FV_MODEL": "r34", "FV_PROFILE": "server"},
    "demo": {"FR_STT_ENGINE": "browser", "FR_LLM_PROVIDER": "nova", "FR_TTS_ENGINE": "browser",
             "FR_SPEAKER_MODULE": "relay.speaker.demo:DemoSpeakerModule"},
}
PROFILE = env("FR_PROFILE", "aws")
if PROFILE not in PROFILES:
    raise ValueError(f"unknown FR_PROFILE={PROFILE!r} ({' | '.join(PROFILES)})")
for _k, _v in PROFILES[PROFILE].items():
    if not os.environ.get(_k):
        os.environ[_k] = _v
STT_ENGINE = env("FR_STT_ENGINE")
TTS_ENGINE = env("FR_TTS_ENGINE")

STORE_BACKEND = env("FR_STORE", "sqlite")  # sqlite | dynamodb
SQLITE_PATH = Path(env("FR_SQLITE_PATH", str(APP_DIR / "data" / "relay.sqlite3")))
DYNAMODB_TABLE = env("FR_DYNAMODB_TABLE", "family-relay")

# FR_LLM_PROVIDER = nova (Amazon Bedrock Nova 2 Lite, set by every profile) | gptproxy (any OpenAI-compatible
# endpoint, what we used while developing)
LLM_PROVIDER = env("FR_LLM_PROVIDER")
# forward each utterance's audio to the LLM provider too (for a future speech-capable model); providers that
# cannot take audio ignore it
LLM_AUDIO_INPUT = env_bool("FR_LLM_AUDIO_INPUT", False)
OPENAI_BASE_URL = env("FR_OPENAI_BASE_URL", "http://127.0.0.1:8788/v1")
OPENAI_MODEL = env("FR_OPENAI_MODEL", "gpt-6-luna")
OPENAI_API_KEY = env("FR_OPENAI_API_KEY")
OPENAI_EFFORT = env("FR_OPENAI_EFFORT", "low")
NOVA_REGION = env("FR_NOVA_REGION", "us-east-1")
NOVA_MODEL = env("FR_NOVA_MODEL", "us.amazon.nova-2-lite-v1:0")
AWS_PROFILE = env("FR_AWS_PROFILE")  # empty = default credential chain (instance / Lambda role)

SPEAKER_MODULE = env("FR_SPEAKER_MODULE")  # "module:Class", set by the profile
MESSAGE_TTL_HOURS = int(env("FR_MESSAGE_TTL_HOURS", "24"))
OBSERVATION_TTL_SEC = int(env("FR_OBSERVATION_TTL_SEC", "600"))
NOTIFY_WEBHOOK_URL = env("FR_NOTIFY_WEBHOOK_URL")  # optional: POST delivery receipts here (e.g. Slack/phone push bridge)

DEMO_MODE = env_bool("FR_DEMO_MODE", True)  # /demo/* clock + reset routes
DEFAULT_TIMEZONE = env("FR_DEFAULT_TIMEZONE", "America/Los_Angeles")
ALLOWED_ORIGINS = [o.strip() for o in env("FR_ALLOWED_ORIGINS").split(",") if o.strip()]
MCP_STATELESS = env_bool("FR_MCP_STATELESS", False)
MCP_JSON_RESPONSE = env_bool("FR_MCP_JSON_RESPONSE", False)
