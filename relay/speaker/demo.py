"""Stand-in speaker module for demos, until the real voice-embedding module is plugged in.

It receives and fingerprints the real audio, but decides WHO is speaking from a demo hint (the simulator's
"who is speaking" switch). It imitates enrollment-free learning:
  - a voice heard for the first time starts a new, unnamed cluster;
  - every usable utterance joins its cluster and confidence grows with cluster size;
  - noisy audio is scored low and discarded (never added to a cluster);
  - after a couple of utterances the cluster's name is "learned" (the real module infers it from
    conversational cues; here the demo hint carries it).
It also returns a 2-D point per utterance so the UI can draw the clusters forming.
"""
from __future__ import annotations

import hashlib
import math
import random
import uuid
from datetime import datetime, timezone
from typing import Any

CONFIRM_AT = 0.75
TENTATIVE_AT = 0.5
NAME_AFTER = 2            # utterances before the cluster's name is known
CLARITY_PENALTY = {"clear": 0.0, "far": 0.14, "noisy": 0.4}


def _center(index: int) -> tuple[float, float]:
    """Spread cluster centres around a ring in the order voices were first heard (stands in for a 2-D projection)."""
    ang = -math.pi / 2 + index * (2 * math.pi / 5)
    return 0.5 + 0.34 * math.cos(ang), 0.56 + 0.26 * math.sin(ang)


class DemoSpeakerModule:
    def __init__(self, household: str, store: Any):
        self.household = household
        self.store = store
        self.pk = f"household#{household}"
        self._hint: dict[str, Any] = {}

    def set_demo_hint(self, hint: dict[str, Any]) -> None:  # demo-only side channel
        self._hint = dict(hint or {})

    def observe(self, audio_bytes: bytes, ts: float) -> dict[str, Any]:
        hint, self._hint = self._hint, {}
        voice = (hint.get("voice") or "").strip()
        clarity = hint.get("clarity") or "clear"
        base = {"module": "demo (switch-driven)", "audio_bytes": len(audio_bytes),
                "audio_sha256": hashlib.sha256(audio_bytes).hexdigest()[:12],
                "thresholds": {"confirmed": CONFIRM_AT, "tentative": TENTATIVE_AT}}
        when = datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
        rnd = random.Random(f"{voice}{ts}")
        if not voice or not audio_bytes:
            return {**base, "speaker_id": None, "state": "unknown", "score": 0.0, "name": None, "discarded": True,
                    "reason": "no speech"}
        rec = self.store.get(self.pk, f"voice#{voice}")
        if rec is None:
            rec = {"voice": voice, "speaker_id": "spk_" + uuid.uuid4().hex[:6], "name": None, "utterances": 0,
                   "index": len(self.store.query(self.pk, "voice#")),
                   "first_heard": when, "evidence": []}
        n = rec["utterances"]
        clean = 0.45 if n == 0 else min(0.64 + 0.07 * n, 0.94)
        score = round(max(0.0, clean - CLARITY_PENALTY.get(clarity, 0.0) + rnd.uniform(-0.02, 0.02)), 3)
        state = "confirmed" if score >= CONFIRM_AT else "tentative" if score >= TENTATIVE_AT else "unknown"
        cx, cy = _center(rec.get("index", 0))
        discarded = clarity == "noisy"
        if discarded:  # far from every cluster, kept out of the model
            ang = rnd.uniform(0, 2 * math.pi)
            x, y = 0.5 + 0.08 * math.cos(ang), 0.5 + 0.08 * math.sin(ang)
        else:
            spread = 0.07 if clarity == "clear" else 0.12
            x, y = cx + rnd.gauss(0, spread / 2), cy + rnd.gauss(0, spread / 2)
            rec["utterances"] = n + 1
            rec["last_heard"], rec["last_score"] = when, score
            if rec["name"] is None and rec["utterances"] >= NAME_AFTER and hint.get("name"):
                rec["name"] = hint["name"]
                rec["evidence"].append(f"{when} name inferred from conversation cues (demo stand-in)")
            self.store.put(self.pk, f"voice#{voice}", rec)
        self.store.put(self.pk, f"vpt#{when}#{uuid.uuid4().hex[:4]}",
                       {"ts": when, "x": round(x, 4), "y": round(y, 4), "speaker_id": None if discarded else rec["speaker_id"],
                        "state": state, "discarded": discarded})
        return {**base, "speaker_id": None if discarded else rec["speaker_id"], "state": "unknown" if discarded else state,
                "score": score, "name": None if discarded else rec["name"], "discarded": discarded,
                "cluster_size": rec["utterances"], "new_cluster": n == 0 and not discarded,
                "viz_xy": [round(x, 4), round(y, 4)]}

    def bind_name(self, speaker_id: str, name: str, evidence: str) -> dict[str, Any]:
        for rec in self.store.query(self.pk, "voice#"):
            if rec["speaker_id"] == speaker_id:
                rec["name"] = name
                rec.setdefault("evidence", []).append(evidence)
                self.store.put(self.pk, f"voice#{rec['voice']}", rec)
                return {"speaker_id": speaker_id, "name": name, "evidence": rec["evidence"]}
        raise KeyError(f"unknown speaker_id {speaker_id}")

    def speakers(self) -> list[dict[str, Any]]:
        out = []
        for r in self.store.query(self.pk, "voice#"):
            cx, cy = _center(r.get("index", 0))
            out.append({"speaker_id": r["speaker_id"], "name": r.get("name"), "utterances": r["utterances"],
                        "first_heard": r.get("first_heard"), "last_heard": r.get("last_heard"),
                        "last_score": r.get("last_score"), "center": [round(cx, 3), round(cy, 3)],
                        "evidence": r.get("evidence", [])[-1:]})
        return out

    def points(self) -> list[dict[str, Any]]:
        return self.store.query(self.pk, "vpt#")[-120:]
