"""Speaker-identification module interface (enrollment-free: voices are clustered as the family uses the device).

The real module (work/speaker/, designed and measured separately) plugs in through FR_SPEAKER_MODULE
("package.module:Class"). The class is constructed once per household:

    Class(household: str, store: relay.store.Store)

and must provide

    observe(audio_bytes: bytes, ts: float) -> {
        "speaker_id": str | None,       # cluster id; None if no voice / too short
        "state": "confirmed" | "tentative" | "unknown",
        "score": float,                 # similarity to the chosen cluster, 0..1
        "name": str | None,             # bound name of that cluster, if any
        ... any extra diagnostic keys (shown in the simulator's speaker panel)
    }
    bind_name(speaker_id: str, name: str, evidence: str) -> dict   # attach a name to a voice cluster
    speakers() -> list[dict]            # optional: known clusters for the status card
                                        #   [{"speaker_id", "name", "utterances", "first_heard", "last_heard"}]

`observe` gets the raw recording the browser captured (webm/opus from MediaRecorder, or a 16 kHz WAV) and the
time (epoch seconds, the household's clock). Only the module decides `state` and `name`. The relay acts for a
person only on "confirmed" with a name; "tentative" and "unknown" mean silence - nobody is ever asked who they
are, and the next utterance is judged again.

Optional: `note_transcript(result, transcript)` - the relay passes each utterance's observe result and its
transcript so the module can learn names from conversation ("Sunhee!", "It's Dad"). The app never calls bind_name.
"""
from __future__ import annotations

import importlib
from typing import Any, Protocol

from .. import config
from ..store import get_store

STATES = ("confirmed", "tentative", "unknown")


class SpeakerModule(Protocol):
    def observe(self, audio_bytes: bytes, ts: float) -> dict[str, Any]: ...
    def bind_name(self, speaker_id: str, name: str, evidence: str) -> dict[str, Any]: ...


_modules: dict[str, SpeakerModule] = {}


def get_speaker_module(household: str) -> SpeakerModule:
    if household not in _modules:
        mod_name, _, cls_name = config.SPEAKER_MODULE.partition(":")
        cls = getattr(importlib.import_module(mod_name), cls_name)
        _modules[household] = cls(household=household, store=get_store())
    return _modules[household]


def reset_speaker_module(household: str) -> None:
    """Forget the household's module instance (its in-memory clusters and pool) - used by the demo reset."""
    _modules.pop(household, None)


def check_result(r: dict[str, Any]) -> dict[str, Any]:
    """Validate a module result against the interface contract (fail loudly on a broken module)."""
    if r.get("state") not in STATES:
        raise ValueError(f"speaker module returned state={r.get('state')!r}, expected one of {STATES}")
    if not isinstance(r.get("score"), (int, float)):
        raise ValueError("speaker module result has no numeric 'score'")
    r.setdefault("speaker_id", None)
    r.setdefault("name", None)
    return r
