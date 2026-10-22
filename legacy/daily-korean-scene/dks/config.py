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
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _scenes_dir() -> Path:
    explicit = env("DKS_SCENES_DIR")
    if explicit:
        return Path(explicit)
    content = APP_DIR.parent / "content" / "scenes"
    if content.is_dir() and any(content.glob("*.json")):
        return content
    return APP_DIR / "fixtures" / "scenes"


SCENES_DIR = _scenes_dir()
# audio/<scene_id>/<line_id>.mp3 sits next to the scenes folder in the content contract
AUDIO_DIR = Path(env("DKS_AUDIO_DIR") or (SCENES_DIR.parent / "audio"))
CATALOG_PATH = Path(env("DKS_CATALOG", str(APP_DIR / "dks" / "catalog.json")))

STORE_BACKEND = env("DKS_STORE", "sqlite")  # sqlite | dynamodb
SQLITE_PATH = Path(env("DKS_SQLITE_PATH", str(APP_DIR / "data" / "dks.sqlite3")))
DYNAMODB_TABLE = env("DKS_DYNAMODB_TABLE", "daily-korean-scene")

LLM_PROVIDER = env("DKS_LLM_PROVIDER", "bedrock")
BEDROCK_REGION = env("DKS_BEDROCK_REGION", "us-east-1")
AWS_PROFILE = env("DKS_AWS_PROFILE")  # empty = default credential chain (Lambda role)
# comma-separated "region|modelId" pools; each region x inference profile has its own requests-per-minute quota
GRADER_MODEL = env("DKS_GRADER_MODEL", ",".join(
    f"{r}|{p}.anthropic.claude-sonnet-4-6" for r in ("us-east-1", "us-west-2") for p in ("global", "us")))
ORCHESTRATOR_MODEL = env("DKS_ORCHESTRATOR_MODEL", ",".join(
    f"{r}|{p}.anthropic.claude-haiku-4-5-20251001-v1:0" for r in ("us-east-1", "us-west-2") for p in ("global", "us")))

DEMO_MODE = env_bool("DKS_DEMO_MODE", True)  # enables /demo/* clock controls
DEFAULT_TIMEZONE = env("DKS_DEFAULT_TIMEZONE", "America/Los_Angeles")
PUBLIC_BASE_URL = env("DKS_PUBLIC_BASE_URL", "")  # used for absolute audio URLs; empty = from request
ALLOWED_ORIGINS = [o.strip() for o in env("DKS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
MCP_STATELESS = env_bool("DKS_MCP_STATELESS", False)
MCP_JSON_RESPONSE = env_bool("DKS_MCP_JSON_RESPONSE", False)
