"""Family Relay state and rules, independent of MCP.

Partition `household#<id>` holds:
  clock                    demo clock offset (minutes)
  person#<pid>             a family member: name, aliases, language they speak to the device
  msg#<id>                 a message or a reply (24 h life; text is wiped on expiry)
  obs#<id>                 a speaker observation (who was heard, how sure)
  notif#<ts>#<id>          outbound notification (delivery receipts, expiry warnings)
  event#<ts>#<id>          timeline event
  voice#... / vpt#...      owned by the demo speaker module

Identity comes only from the speaker module (no enrollment, no questions). A person is "settled" for a turn
when the module says `confirmed` and has a name for the voice. Only a settled person receives message content
or status about their own messages; for anyone else the relay stays silent and tries again on the next
utterance.
"""
from __future__ import annotations

import json
import re
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import config
from .speaker import check_result, get_speaker_module, reset_speaker_module
from .store import get_store


class RelayError(Exception):
    """A request the household state cannot satisfy; the message is meant for the assistant."""


def _pk(h: str) -> str:
    return f"household#{h}"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------- clock
def now(h: str) -> datetime:
    c = get_store().get(_pk(h), "clock") or {}
    return datetime.now(timezone.utc) + timedelta(minutes=c.get("offset_min", 0))


def tz(h: str) -> ZoneInfo:
    c = get_store().get(_pk(h), "clock") or {}
    return ZoneInfo(c.get("timezone", config.DEFAULT_TIMEZONE))


def clock_view(h: str) -> dict[str, Any]:
    n = now(h)
    local = n.astimezone(tz(h))
    c = get_store().get(_pk(h), "clock") or {}
    return {"now_utc": n.isoformat(timespec="seconds"), "local": local.isoformat(timespec="minutes"),
            "local_label": local.strftime("%a %b %d, %I:%M %p").replace(" 0", " "),
            "timezone": str(tz(h)), "demo_offset_min": c.get("offset_min", 0)}


def advance_clock(h: str, minutes: int) -> dict[str, Any]:
    st = get_store()
    c = st.get(_pk(h), "clock") or {"offset_min": 0}
    c["offset_min"] = c.get("offset_min", 0) + minutes
    st.put(_pk(h), "clock", c)
    sweep(h)
    return clock_view(h)


def set_clock(h: str, local_hhmm: str) -> dict[str, Any]:
    """Move the demo clock forward to the next occurrence of local HH:MM."""
    hh, mm = (int(x) for x in local_hhmm.split(":"))
    local = now(h).astimezone(tz(h))
    target = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return advance_clock(h, -int(-(target - local).total_seconds() // 60))  # round up to land on HH:MM


def reset_household(h: str) -> None:
    get_store().delete_partition(_pk(h))
    reset_speaker_module(h)


def _label(h: str, iso: str | None) -> str | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso).astimezone(tz(h)).strftime("%I:%M %p").lstrip("0")


# ---------------------------------------------------------------- events & notifications
def _event(h: str, kind: str, text: str, **data: Any) -> None:
    ts = now(h).isoformat(timespec="seconds")
    get_store().put(_pk(h), f"event#{ts}#{uuid.uuid4().hex[:4]}", {"ts": ts, "kind": kind, "text": text, **data})


def _notify(h: str, to_name: str | None, text: str, kind: str) -> dict[str, Any]:
    """Cross-service hand-off: a phone notification (outbox) and, if configured, a webhook POST."""
    ts = now(h).isoformat(timespec="seconds")
    n = {"id": _id("ntf"), "ts": ts, "kind": kind, "to": to_name, "channel": "phone push (companion app)",
         "text": text, "webhook": None}
    if config.NOTIFY_WEBHOOK_URL:
        req = urllib.request.Request(config.NOTIFY_WEBHOOK_URL, data=json.dumps({"household": h, **n}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                n["webhook"] = f"HTTP {r.status}"
        except Exception as e:  # recorded on the notification and shown in the timeline
            n["webhook"] = f"failed: {type(e).__name__}"
    get_store().put(_pk(h), f"notif#{ts}#{n['id']}", n)
    _event(h, "notification", text, to=to_name)
    return n


# ---------------------------------------------------------------- people
def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^(my|our)\s+", "", (name or "").strip().lower()))


def people(h: str) -> list[dict[str, Any]]:
    return get_store().query(_pk(h), "person#")


def find_person(h: str, name: str | None) -> dict[str, Any] | None:
    if not name:
        return None
    q = _norm(name)
    for p in people(h):
        if q == _norm(p["name"]) or q in (_norm(a) for a in p.get("aliases", [])):
            return p
    return None


def _save_person(h: str, p: dict[str, Any]) -> None:
    get_store().put(_pk(h), f"person#{p['person_id']}", p)


def ensure_person(h: str, name: str) -> dict[str, Any]:
    p = find_person(h, name)
    if p:
        return p
    clean = re.sub(r"^(my|our)\s+", "", name.strip(), flags=re.I)
    p = {"person_id": _id("p"), "name": clean[:1].upper() + clean[1:], "aliases": [], "language": None,
         "created_at": now(h).isoformat(timespec="seconds")}
    _save_person(h, p)
    return p


# ---------------------------------------------------------------- speaker observations
def observe(h: str, audio: bytes, ts: float, lang: str | None, mime: str, source: str,
            transcript: str | None, demo_hint: dict[str, Any] | None) -> dict[str, Any]:
    module = get_speaker_module(h)
    if demo_hint and hasattr(module, "set_demo_hint"):
        module.set_demo_hint(demo_hint)
    # the household clock (real time, or the demo clock) - the module separates conversations and call/answer
    # pairs by these times, so they must move with the demo clock rather than the capture time
    result = check_result(module.observe(audio, now(h).timestamp()))
    if transcript and hasattr(module, "note_transcript"):
        module.note_transcript(result, transcript)  # optional cue channel for name learning (module decides)
    person = settled_person(h, result)
    if person and lang and person.get("language") != lang:
        person["language"] = lang
        _save_person(h, person)
    obs = {"observation_id": _id("obs"), "ts": now(h).isoformat(timespec="seconds"), "lang": lang, "mime": mime,
           "source": source, "audio_bytes": len(audio), "transcript": transcript, "result": result,
           "person": person["name"] if person else None}
    sweep(h)
    obs["waiting"] = _waiting_count(h, person)
    obs["household_names"] = sorted({p["name"] for p in people(h)} |
                                    {v["name"] for v in (module.speakers() if hasattr(module, "speakers") else []) if v.get("name")})
    get_store().put(_pk(h), f"obs#{obs['observation_id']}", obs)
    return obs


def settled_person(h: str, result: dict[str, Any]) -> dict[str, Any] | None:
    """The family member this voice certainly is, or None (unsure, unnamed, or no voice)."""
    if result.get("state") != "confirmed" or not result.get("name"):
        return None
    return ensure_person(h, result["name"])


def get_observation(h: str, observation_id: str | None) -> dict[str, Any] | None:
    st = get_store()
    if observation_id:
        o = st.get(_pk(h), f"obs#{observation_id}")
        if o:
            return o
    obs = sorted(st.query(_pk(h), "obs#"), key=lambda o: o["ts"])
    if not obs:
        return None
    last = obs[-1]
    age = (now(h) - datetime.fromisoformat(last["ts"])).total_seconds()
    return last if age <= config.OBSERVATION_TTL_SEC else None


def _speaker(h: str, obs: dict[str, Any] | None) -> dict[str, Any] | None:
    return find_person(h, obs.get("person")) if obs else None


# ---------------------------------------------------------------- messages
def _messages(h: str) -> list[dict[str, Any]]:
    return sorted(get_store().query(_pk(h), "msg#"), key=lambda m: m["created_at"])


def _save_msg(h: str, m: dict[str, Any]) -> None:
    get_store().put(_pk(h), f"msg#{m['id']}", m)


def sweep(h: str) -> None:
    """Expire messages older than the TTL: wipe their text, tell the sender if it was never heard."""
    n = now(h).isoformat(timespec="seconds")
    for m in _messages(h):
        if m["status"] != "expired" and m["expires_at"] <= n:
            unheard = m["status"] == "pending"
            m.update({"status": "expired", "text": None, "expired_at": n})
            _save_msg(h, m)
            _event(h, "expired", f"{m['kind'].capitalize()} from {m['sender_name'] or 'someone'} to "
                   f"{m['recipient_name']} expired" + (" unheard" if unheard else ""), msg_id=m["id"])
            if unheard and m["kind"] == "message" and m["sender_name"]:
                _notify(h, m["sender_name"], f"Your message to {m['recipient_name']} expired before they heard it.",
                        "expired_unheard")


def _msg_view(h: str, m: dict[str, Any], reveal: bool) -> dict[str, Any]:
    v = {k: m.get(k) for k in ("id", "kind", "sender_name", "recipient_name", "status", "condition", "lang",
                               "created_at", "expires_at", "delivered_at", "in_reply_to", "delivered_score")}
    v.update({"created_label": _label(h, m["created_at"]), "delivered_label": _label(h, m.get("delivered_at")),
              "expires_label": _label(h, m["expires_at"]),
              "expires_in_min": max(0, int((datetime.fromisoformat(m["expires_at"]) - now(h)).total_seconds() // 60)),
              "text": m.get("text") if reveal else None, "private": not reveal and m.get("text") is not None,
              "replies": list(m.get("replies", []))})
    return v


def _deliverable(h: str, m: dict[str, Any]) -> bool:
    return m["status"] == "pending" and (not m.get("deliver_after") or m["deliver_after"] <= now(h).isoformat())


def _waiting_count(h: str, person: dict[str, Any] | None) -> int:
    if not person:
        return 0
    return sum(1 for m in _messages(h) if m["recipient_id"] == person["person_id"] and _deliverable(h, m))


def leave_message(h: str, obs: dict[str, Any] | None, recipient: str, text: str, condition: str | None,
                  deliver_after_local: str | None) -> dict[str, Any]:
    sender = _speaker(h, obs)
    if not text.strip():
        raise RelayError("The message is empty.")
    rec = ensure_person(h, recipient)
    if sender and rec["person_id"] == sender["person_id"]:
        raise RelayError("That message is addressed to the person leaving it.")
    created = now(h)
    deliver_after = None
    if deliver_after_local:
        hh, mm = (int(x) for x in deliver_after_local.split(":"))
        local = created.astimezone(tz(h))
        t = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        deliver_after = (t if t > local else t + timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
    m = {"id": _id("msg"), "kind": "message", "text": text.strip(), "lang": (obs or {}).get("lang"),
         "sender_id": sender["person_id"] if sender else None, "sender_name": sender["name"] if sender else None,
         "sender_speaker_id": ((obs or {}).get("result") or {}).get("speaker_id"),
         "recipient_id": rec["person_id"], "recipient_name": rec["name"], "condition": condition,
         "deliver_after": deliver_after, "created_at": created.isoformat(timespec="seconds"),
         "expires_at": (created + timedelta(hours=config.MESSAGE_TTL_HOURS)).isoformat(timespec="seconds"),
         "status": "pending", "replies": []}
    _save_msg(h, m)
    _event(h, "left", f"{m['sender_name'] or 'Someone'} left a message for {rec['name']}", msg_id=m["id"])
    return {"status": "saved", "message": _msg_view(h, m, reveal=True), "clock": clock_view(h)}


def deliver_pending(h: str, obs: dict[str, Any] | None) -> dict[str, Any]:
    """Hand over everything waiting for the settled speaker. Unsettled voice -> nothing, silently."""
    person = _speaker(h, obs)
    base = {"clock": clock_view(h)}
    if person is None:
        return {**base, "status": "nothing", "messages": []}
    delivered = []
    for m in _messages(h):
        if m["recipient_id"] != person["person_id"] or not _deliverable(h, m):
            continue
        m.update({"status": "delivered", "delivered_at": now(h).isoformat(timespec="seconds"),
                  "delivered_score": obs["result"]["score"], "delivered_observation": obs["observation_id"]})
        _save_msg(h, m)
        _event(h, "delivered", f"{m['kind'].capitalize()} from {m['sender_name'] or 'someone'} passed to "
               f"{person['name']} (voice {obs['result']['score']:.2f})", msg_id=m["id"])
        if m["kind"] == "message" and m["sender_name"]:
            _notify(h, m["sender_name"], f"{person['name']} heard your message at {_label(h, m['delivered_at'])}.",
                    "delivery_receipt")
        delivered.append(_msg_view(h, m, reveal=True))
    held = [_msg_view(h, m, reveal=False) for m in _messages(h)
            if m["recipient_id"] == person["person_id"] and m["status"] == "pending"]
    return {**base, "status": "delivered" if delivered else "nothing", "messages": delivered,
            "recipient": {"name": person["name"], "language": person.get("language")}, "held_until_later": held}


def reply_to_message(h: str, obs: dict[str, Any] | None, text: str, message_id: str | None) -> dict[str, Any]:
    person = _speaker(h, obs)
    if person is None:
        return {"status": "not_settled", "clock": clock_view(h)}
    msgs = [m for m in _messages(h) if m["recipient_id"] == person["person_id"] and m["status"] == "delivered"
            and m["kind"] == "message"]
    if message_id:
        msgs = [m for m in msgs if m["id"] == message_id]
    if not msgs:
        raise RelayError(f"There is no message to {person['name']} to reply to.")
    orig = sorted(msgs, key=lambda m: m["delivered_at"])[-1]
    if not orig.get("sender_id"):
        raise RelayError("That message has no known sender to reply to.")
    created = now(h)
    reply = {"id": _id("rep"), "kind": "reply", "text": text.strip(), "lang": obs.get("lang"),
             "sender_id": person["person_id"], "sender_name": person["name"],
             "sender_speaker_id": obs["result"].get("speaker_id"),
             "recipient_id": orig["sender_id"], "recipient_name": orig["sender_name"], "condition": None,
             "deliver_after": None, "created_at": created.isoformat(timespec="seconds"),
             "expires_at": (created + timedelta(hours=config.MESSAGE_TTL_HOURS)).isoformat(timespec="seconds"),
             "status": "pending", "replies": [], "in_reply_to": orig["id"]}
    _save_msg(h, reply)
    orig["replies"].append({"id": reply["id"], "at": reply["created_at"], "at_label": _label(h, reply["created_at"])})
    _save_msg(h, orig)
    _event(h, "reply", f"{person['name']} replied to {orig['sender_name']}", msg_id=reply["id"])
    return {"status": "reply_saved", "reply": _msg_view(h, reply, reveal=True), "original": _msg_view(h, orig, reveal=True),
            "clock": clock_view(h)}


def ask_status(h: str, obs: dict[str, Any] | None, about: str) -> dict[str, Any]:
    asker = _speaker(h, obs)
    target = find_person(h, about)
    if asker is None:
        return {"status": "not_settled", "about": about, "messages": [], "clock": clock_view(h)}
    if target is None:
        return {"status": "no_messages", "about": about, "asker": asker["name"], "messages": [], "clock": clock_view(h)}
    sent = [m for m in _messages(h) if m["kind"] == "message" and m["sender_id"] == asker["person_id"]
            and m["recipient_id"] == target["person_id"]]
    if not sent:
        return {"status": "no_messages", "about": target["name"], "asker": asker["name"], "messages": [], "clock": clock_view(h)}
    out = []
    for m in sent[-3:]:
        v = _msg_view(h, m, reveal=True)
        replies = []
        for ref in m.get("replies", []):
            rep = get_store().get(_pk(h), f"msg#{ref['id']}")
            if not rep or rep["status"] == "expired":
                continue
            if rep["status"] == "pending":  # the asker hears it now: that is the reply's delivery
                rep.update({"status": "delivered", "delivered_at": now(h).isoformat(timespec="seconds"),
                            "delivered_score": obs["result"]["score"]})
                _save_msg(h, rep)
                _event(h, "delivered", f"Reply from {rep['sender_name']} passed to {asker['name']}", msg_id=rep["id"])
            replies.append(_msg_view(h, rep, reveal=True))
        v["reply_messages"] = replies
        out.append(v)
    return {"status": "found", "about": target["name"], "asker": asker["name"], "messages": out, "clock": clock_view(h)}


# ---------------------------------------------------------------- memory & status views
def family_memory(h: str, obs: dict[str, Any] | None) -> dict[str, Any]:
    viewer = _speaker(h, obs)
    items = [_msg_view(h, m, reveal=bool(viewer) and viewer["person_id"] in (m["sender_id"], m["recipient_id"]))
             for m in _messages(h)]
    events = sorted(get_store().query(_pk(h), "event#"), key=lambda e: e["ts"])[-30:]
    for e in events:
        e["ts_label"] = _label(h, e["ts"])
    notifs = sorted(get_store().query(_pk(h), "notif#"), key=lambda n: n["ts"])[-10:]
    for n in notifs:
        n["ts_label"] = _label(h, n["ts"])
    live = [i for i in items if i["status"] != "expired"]
    return {"viewer": viewer["name"] if viewer else None, "live": live,
            "expired": [i for i in items if i["status"] == "expired"][-5:],
            "expiring_soon": sorted([i for i in live if i["expires_in_min"] <= 180], key=lambda i: i["expires_in_min"]),
            "people": [{"name": p["name"], "language": p.get("language")} for p in people(h)],
            "events": events, "notifications": notifs, "ttl_hours": config.MESSAGE_TTL_HOURS, "clock": clock_view(h)}


def speaker_status(h: str, obs: dict[str, Any] | None) -> dict[str, Any]:
    module = get_speaker_module(h)
    voices = module.speakers() if hasattr(module, "speakers") else []
    points = module.points() if hasattr(module, "points") else []
    recent = sorted(get_store().query(_pk(h), "obs#"), key=lambda o: o["ts"])[-10:]
    return {"current": obs, "voices": voices, "points": points,
            "recent": [{"ts_label": _label(h, o["ts"]), "speaker_id": o["result"].get("speaker_id"),
                        "name": o["result"].get("name"), "state": o["result"]["state"], "score": o["result"]["score"],
                        "discarded": o["result"].get("discarded", False), "transcript": o.get("transcript")}
                       for o in recent],
            "clock": clock_view(h)}
