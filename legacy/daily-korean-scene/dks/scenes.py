"""Scene content (read-only) and the pack catalog."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import config


@dataclass(frozen=True)
class Scene:
    data: dict[str, Any]

    @property
    def id(self) -> str:
        return self.data["scene_id"]

    @property
    def lines(self) -> list[dict[str, Any]]:
        return self.data["lines"]

    def character(self, cid: str) -> dict[str, Any] | None:
        for c in self.data.get("characters", []):
            if c["id"] == cid:
                return c
        return None

    def learner_line_indexes(self) -> list[int]:
        return [i for i, line in enumerate(self.lines) if line["speaker"] == "learner"]

    def review_targets_for(self, line: dict[str, Any]) -> list[dict[str, Any]]:
        """Review targets whose expression appears in this line's model answer."""
        texts = [line.get("ko") or ""] + list(line.get("expected") or [])
        compact = [t.replace(" ", "") for t in texts]
        out = []
        for t in self.data.get("review_targets", []):
            expr = t["expr_ko"].replace(" ", "")
            if any(expr in c for c in compact):
                out.append(t)
        return out


def validate_scene(data: dict[str, Any], source: str) -> None:
    for key in ("scene_id", "title_en", "title_ko", "lines"):
        if key not in data:
            raise ValueError(f"{source}: missing '{key}'")
    ids = {c["id"] for c in data.get("characters", [])}
    for line in data["lines"]:
        sp = line.get("speaker")
        if sp not in ids and sp not in ("learner", "narrator"):
            raise ValueError(f"{source}: line {line.get('id')} has unknown speaker {sp!r}")
        if sp == "learner" and not line.get("expected"):
            raise ValueError(f"{source}: learner line {line.get('id')} has no 'expected' answers")


@lru_cache(maxsize=1)
def load_scenes() -> dict[str, Scene]:
    scenes: dict[str, Scene] = {}
    for path in sorted(Path(config.SCENES_DIR).glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        validate_scene(data, path.name)
        scenes[data["scene_id"]] = Scene(data)
    if not scenes:
        raise RuntimeError(f"no scenes found in {config.SCENES_DIR}")
    return scenes


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Any]:
    raw = json.loads(Path(config.CATALOG_PATH).read_text(encoding="utf-8"))
    scenes = load_scenes()
    packs = []
    pack_scene_ids: set[str] = set()
    for p in raw.get("packs", []):
        present = [s for s in p["scene_ids"] if s in scenes]
        pack_scene_ids.update(p["scene_ids"])
        packs.append({**p, "available_scene_ids": present,
                      "coming_soon": len(p["scene_ids"]) - len(present)})
    free = [s for s in raw.get("free_scene_order", []) if s in scenes]
    for sid in scenes:
        if sid not in free and sid not in pack_scene_ids:
            free.append(sid)
    return {"free_scene_order": free, "packs": packs}


def get_pack(pack_id: str) -> dict[str, Any] | None:
    for p in load_catalog()["packs"]:
        if p["pack_id"] == pack_id:
            return p
    return None


def find_pack(query: str) -> dict[str, Any] | None:
    """Match a pack by id or by (partial, case-insensitive) title, as a voice user would say it."""
    q = query.strip().lower().replace("-", " ")
    for p in load_catalog()["packs"]:
        if q in (p["pack_id"].replace("-", " "), p["title_en"].lower()) or q == p["title_ko"]:
            return p
    for p in load_catalog()["packs"]:
        if q and (q in p["title_en"].lower() or q in p["pack_id"].replace("-", " ")):
            return p
    return None


def audio_path(scene_id: str, line: dict[str, Any]) -> Path | None:
    """The line's audio file, or None (the client then falls back to TTS).

    'audio' null means "no approved recording" (e.g. it failed the hallucination check), so a file
    lying at the contract location is NOT used then. A set path is resolved relative to the content
    folder, then as the contract location audio/<scene_id>/<line_id>.mp3."""
    rel = line.get("audio")
    if not rel:
        return None
    p = Path(rel) if Path(rel).is_absolute() else Path(config.AUDIO_DIR).parent / rel
    if p.exists():
        return p
    alt = Path(config.AUDIO_DIR) / scene_id / f"{line['id']}.mp3"
    return alt if alt.exists() else None
