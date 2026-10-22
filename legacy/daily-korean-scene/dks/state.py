"""Learner state and the learning flow, independent of MCP.

Every learner lives in one partition `learner#<id>`:
  profile            name, timezone, demo clock offset, streak, xp
  activity           the scene turn submit_learner_turn answers
  review_activity    the review session submit_review_answer answers
  day#<YYYY-MM-DD>   the daily scene record for that local day
  review#<item_id>   spaced-repetition card (Leitner boxes)
  purchase#<token>   scene-pack purchase (pending -> confirmed | cancelled)
  schedule#practice  the practice schedule
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import config, grader
from .scenes import Scene, audio_path, find_pack, get_pack, load_catalog, load_scenes
from .store import get_store

LEITNER_DAYS = [1, 2, 4, 7, 15, 30]   # box n -> next review after n days
MAX_TRIES_PER_TURN = 2                 # an 'incorrect' answer may be retried once
PURCHASE_TTL_MIN = 10
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class FlowError(Exception):
    """A request the learner's current state cannot satisfy; the message is meant for the assistant."""


def _pk(lid: str) -> str:
    return f"learner#{lid}"


# ---------------------------------------------------------------- profile & demo clock
def profile(lid: str) -> dict[str, Any]:
    st = get_store()
    p = st.get(_pk(lid), "profile")
    if p is None:
        p = {"learner_id": lid, "timezone": config.DEFAULT_TIMEZONE, "day_offset": 0, "streak": 0,
             "best_streak": 0, "last_completed_day": None, "xp": 0,
             "created_at": datetime.now(timezone.utc).isoformat()}
        st.put(_pk(lid), "profile", p)
    return p


def _save_profile(p: dict[str, Any]) -> None:
    get_store().put(_pk(p["learner_id"]), "profile", p)


def now_utc(p: dict[str, Any]) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=p.get("day_offset", 0))


def today(p: dict[str, Any]) -> date:
    return now_utc(p).astimezone(ZoneInfo(p["timezone"])).date()


def clock_view(p: dict[str, Any]) -> dict[str, Any]:
    local = now_utc(p).astimezone(ZoneInfo(p["timezone"]))
    return {"now_utc": now_utc(p).isoformat(timespec="seconds"), "local_date": local.date().isoformat(),
            "local_time": local.strftime("%H:%M"), "weekday": WEEKDAYS[local.weekday()],
            "timezone": p["timezone"], "demo_day_offset": p.get("day_offset", 0)}


def advance_days(lid: str, days: int = 1) -> dict[str, Any]:
    p = profile(lid)
    p["day_offset"] = p.get("day_offset", 0) + days
    _save_profile(p)
    # an unfinished activity belongs to the old day
    get_store().delete(_pk(lid), "activity")
    get_store().delete(_pk(lid), "review_activity")
    return clock_view(p)


def reset_learner(lid: str) -> None:
    get_store().delete_partition(_pk(lid))


# ---------------------------------------------------------------- helpers
def _available_scene_ids(lid: str) -> list[str]:
    cat = load_catalog()
    ids = list(cat["free_scene_order"])
    for pack_id in owned_packs(lid):
        pack = get_pack(pack_id)
        if pack:
            ids += [s for s in pack["available_scene_ids"] if s not in ids]
    return ids


def owned_packs(lid: str) -> list[str]:
    return [d["pack_id"] for d in get_store().query(_pk(lid), "purchase#") if d["status"] == "confirmed"]


def _line_view(scene: Scene, line: dict[str, Any], base_url: str) -> dict[str, Any]:
    sp = line["speaker"]
    ch = scene.character(sp) if sp not in ("learner", "narrator") else None
    audio = audio_path(scene.id, line)
    return {
        "id": line["id"], "speaker": sp,
        "speaker_name_ko": ch["name_ko"] if ch else ("나" if sp == "learner" else ""),
        # role_en may be a long casting note ("Barista - early 20s, cheerful ..."): show its head only
        "speaker_role_en": re.split(r" - |, |\(", ch["role_en"])[0].strip() if ch else ("You" if sp == "learner" else "Narrator"),
        "voice": ch.get("voice") if ch else None,
        "ko": line.get("ko") or "", "romanization": line.get("romanization") or "", "en": line.get("en") or "",
        "audio_url": f"{base_url}/audio/{scene.id}/{audio.name}" if audio else None,
    }


def _prompt_view(scene: Scene, idx: int, tries: int) -> dict[str, Any]:
    line = scene.lines[idx]
    learner_idx = scene.learner_line_indexes()
    return {"line_id": line["id"], "task_en": line["en"], "hint_en": line.get("hints_en") or "",
            "turn": learner_idx.index(idx) + 1, "turns_total": len(learner_idx), "tries_used": tries}


def _scene_meta(scene: Scene) -> dict[str, Any]:
    d = scene.data
    return {"scene_id": d["scene_id"], "title_en": d["title_en"], "title_ko": d["title_ko"],
            "level": d.get("level", ""), "setting_en": d.get("setting_en", ""),
            "characters": d.get("characters", [])}


def _play_until_learner(scene: Scene, start: int, base_url: str) -> tuple[list[dict[str, Any]], int | None]:
    """Lines from `start` up to the next learner line. Returns (spoken lines, learner index or None at the end)."""
    out = []
    i = start
    while i < len(scene.lines):
        if scene.lines[i]["speaker"] == "learner":
            return out, i
        out.append(_line_view(scene, scene.lines[i], base_url))
        i += 1
    return out, None


def _due_reviews(lid: str, day: date) -> list[dict[str, Any]]:
    items = get_store().query(_pk(lid), "review#")
    due = [i for i in items if i["due_day"] <= day.isoformat()]
    return sorted(due, key=lambda i: (i["box"], i["due_day"], i["created_at"]))


# ---------------------------------------------------------------- daily scene
def start_daily_scene(lid: str, base_url: str, scene_id: str | None = None, replay: bool = False) -> dict[str, Any]:
    p = profile(lid)
    st = get_store()
    day = today(p)
    scenes = load_scenes()
    available = _available_scene_ids(lid)
    rec = st.get(_pk(lid), f"day#{day.isoformat()}")

    if scene_id:
        if scene_id not in scenes:
            raise FlowError(f"Unknown scene '{scene_id}'. Available scenes: {', '.join(available)}.")
        if scene_id not in available:
            pack = next((pk for pk in load_catalog()["packs"] if scene_id in pk["scene_ids"]), None)
            return {"status": "locked", "scene": _scene_meta(scenes[scene_id]),
                    "pack": _pack_view(pack, lid) if pack else None, "clock": clock_view(p)}
    if rec and not scene_id:
        if rec["status"] == "completed" and not replay:
            return {"status": "completed_today", "scene": _scene_meta(scenes[rec["scene_id"]]),
                    "summary": rec.get("summary"), "review_due": len(_due_reviews(lid, day)), "clock": clock_view(p)}
        if rec["status"] == "in_progress":
            act = st.get(_pk(lid), "activity")
            if act and act.get("kind") == "scene" and act["day"] == day.isoformat():
                scene = scenes[rec["scene_id"]]
                history = [_line_view(scene, ln, base_url) for ln in scene.lines[:act["cursor"]]
                           if ln["speaker"] != "learner"]
                return {"status": "resumed", "scene": _scene_meta(scene), "day_number": rec["day_number"],
                        "lines": history[-3:], "prompt": _prompt_view(scene, act["cursor"], act.get("tries", 0)),
                        "review_due": len(_due_reviews(lid, day)), "clock": clock_view(p)}
        if replay or rec["status"] == "in_progress":
            scene_id = rec["scene_id"]

    if not scene_id:
        done = {d["scene_id"]: d.get("completed_at") or "" for d in st.query(_pk(lid), "day#")
                if d["status"] == "completed"}
        fresh = [s for s in available if s not in done]
        scene_id = fresh[0] if fresh else min(available, key=lambda s: done.get(s, ""))

    scene = scenes[scene_id]
    day_number = len({d["date"] for d in st.query(_pk(lid), "day#")} - {day.isoformat()}) + 1
    rec = {"date": day.isoformat(), "scene_id": scene_id, "status": "in_progress", "day_number": day_number,
           "started_at": now_utc(p).isoformat(), "attempts": []}
    st.put(_pk(lid), f"day#{day.isoformat()}", rec)
    lines, learner_idx = _play_until_learner(scene, 0, base_url)
    st.put(_pk(lid), "activity", {"kind": "scene", "day": day.isoformat(), "scene_id": scene_id,
                                  "cursor": learner_idx, "tries": 0})
    return {"status": "started", "scene": _scene_meta(scene), "day_number": day_number, "lines": lines,
            "prompt": _prompt_view(scene, learner_idx, 0) if learner_idx is not None else None,
            "review_due": len(_due_reviews(lid, day)), "clock": clock_view(p)}


def _previous_spoken(scene: Scene, idx: int) -> dict[str, str] | None:
    for j in range(idx - 1, -1, -1):
        ln = scene.lines[j]
        if ln["speaker"] not in ("learner", "narrator"):
            return {"ko": ln["ko"], "en": ln["en"]}
    return None


def _upsert_review(lid: str, p: dict[str, Any], scene: Scene, line: dict[str, Any], answer: str,
                   g: dict[str, Any]) -> dict[str, Any]:
    st = get_store()
    item_id = f"{scene.id}.{line['id']}"
    item = st.get(_pk(lid), f"review#{item_id}") or {
        "item_id": item_id, "scene_id": scene.id, "line_id": line["id"], "created_at": now_utc(p).isoformat(),
        "prompt_en": line["en"], "hint_en": line.get("hints_en") or "", "answer_ko": line["ko"],
        "answer_romanization": line.get("romanization") or "", "accepted": line["expected"],
        "setting_en": scene.data.get("setting_en", ""), "previous_line": _previous_spoken(scene, scene.lines.index(line)),
        "lapses": 0, "reviews": 0,
    }
    targets = scene.review_targets_for(line)
    focus = g.get("focus_expression_ko") or ""
    target = next((t for t in targets if focus and t["expr_ko"].replace(" ", "") in focus.replace(" ", "")), None) \
        or (targets[0] if targets else None)
    item.update({"box": 0, "due_day": (today(p) + timedelta(days=LEITNER_DAYS[0])).isoformat(),
                 "last_answer": answer, "last_verdict": g["verdict"], "last_corrected": g.get("corrected_ko", ""),
                 "last_issues": g.get("issues", []), "focus_ko": target["expr_ko"] if target else focus,
                 "focus_en": target["en"] if target else "", "focus_note": target["note"] if target else ""})
    item["lapses"] += 1
    st.put(_pk(lid), f"review#{item_id}", item)
    return item


def submit_learner_turn(lid: str, answer_ko: str, base_url: str) -> dict[str, Any]:
    st = get_store()
    act = st.get(_pk(lid), "activity")
    if not act:
        raise FlowError("No scene turn is waiting for an answer. Start today's scene with start_daily_scene first "
                        "(review answers go to submit_review_answer).")
    return _submit_scene(lid, act, answer_ko, base_url)


def submit_review_answer(lid: str, answer_ko: str) -> dict[str, Any]:
    act = get_store().get(_pk(lid), "review_activity")
    if not act:
        raise FlowError("No review card is waiting for an answer. Call get_review_queue first.")
    return _submit_review(lid, act, answer_ko)


def _submit_scene(lid: str, act: dict[str, Any], answer: str, base_url: str) -> dict[str, Any]:
    p = profile(lid)
    st = get_store()
    scene = load_scenes()[act["scene_id"]]
    idx = act["cursor"]
    line = scene.lines[idx]
    g = grader.grade(answer=answer, task_en=line["en"], model_answers=line["expected"], model_ko=line["ko"],
                     model_romanization=line.get("romanization") or "", scene_setting=scene.data.get("setting_en") or "",
                     previous_line=_previous_spoken(scene, idx))
    tries = act.get("tries", 0) + 1
    rec = st.get(_pk(lid), f"day#{act['day']}")
    rec["attempts"].append({"line_id": line["id"], "answer": answer, "verdict": g["verdict"], "score": g["score"],
                            "at": now_utc(p).isoformat()})
    review_item = None
    if g["verdict"] != "correct":
        review_item = _upsert_review(lid, p, scene, line, answer, g)
    p["xp"] = p.get("xp", 0) + {"correct": 10, "close": 5}.get(g["verdict"], 1)

    result: dict[str, Any] = {
        "scene": _scene_meta(scene), "day_number": rec["day_number"], "answer_ko": answer, "grade": g,
        "model_line": _line_view(scene, line, base_url), "tries_used": tries,
        "added_to_review": bool(review_item),
        "review_focus": {"ko": review_item["focus_ko"], "en": review_item["focus_en"]} if review_item else None,
    }
    if g["verdict"] == "incorrect" and tries < MAX_TRIES_PER_TURN:
        act["tries"] = tries
        st.put(_pk(lid), "activity", act)
        st.put(_pk(lid), f"day#{act['day']}", rec)
        _save_profile(p)
        result.update({"status": "try_again", "lines": [], "prompt": _prompt_view(scene, idx, tries)})
        return result

    lines, nxt = _play_until_learner(scene, idx + 1, base_url)
    result["lines"] = lines
    if nxt is None:
        result.update({"status": "scene_complete", "prompt": None, "summary": _complete_day(p, rec)})
        st.delete(_pk(lid), "activity")
    else:
        st.put(_pk(lid), "activity", {**act, "cursor": nxt, "tries": 0})
        result.update({"status": "next_turn", "prompt": _prompt_view(scene, nxt, 0)})
    st.put(_pk(lid), f"day#{act['day']}", rec)
    _save_profile(p)
    return result


def _complete_day(p: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any]:
    last_by_line: dict[str, dict[str, Any]] = {}
    for a in rec["attempts"]:
        last_by_line.setdefault(a["line_id"], a)  # first attempt counts for accuracy
    firsts = list(last_by_line.values())
    correct = sum(1 for a in firsts if a["verdict"] == "correct")
    day = date.fromisoformat(rec["date"])
    last = p.get("last_completed_day")
    if last == (day - timedelta(days=1)).isoformat():
        p["streak"] = p.get("streak", 0) + 1
    elif last != day.isoformat():
        p["streak"] = 1
    p["best_streak"] = max(p.get("best_streak", 0), p["streak"])
    p["last_completed_day"] = day.isoformat()
    summary = {"turns": len(firsts), "correct_first_try": correct,
               "to_review": [a["line_id"] for a in firsts if a["verdict"] != "correct"],
               "streak": p["streak"], "xp": p["xp"]}
    rec.update({"status": "completed", "completed_at": now_utc(p).isoformat(), "summary": summary})
    return summary


# ---------------------------------------------------------------- spaced-repetition review
def _review_card(item: dict[str, Any], position: int, total: int, reveal: bool) -> dict[str, Any]:
    card = {"item_id": item["item_id"], "position": position, "total": total, "prompt_en": item["prompt_en"],
            "hint_en": item["hint_en"], "focus_en": item.get("focus_en", ""), "box": item["box"],
            "last_answer": item.get("last_answer", ""), "scene_id": item["scene_id"]}
    if reveal:
        card.update({"answer_ko": item["answer_ko"], "answer_romanization": item["answer_romanization"],
                     "focus_ko": item.get("focus_ko", ""), "focus_note": item.get("focus_note", "")})
    return card


def get_review_queue(lid: str) -> dict[str, Any]:
    p = profile(lid)
    day = today(p)
    due = _due_reviews(lid, day)
    all_items = get_store().query(_pk(lid), "review#")
    if not due:
        upcoming = sorted(i["due_day"] for i in all_items)
        get_store().delete(_pk(lid), "review_activity")
        return {"status": "nothing_due", "cards": [], "total_items": len(all_items),
                "next_due_day": upcoming[0] if upcoming else None, "clock": clock_view(p)}
    get_store().put(_pk(lid), "review_activity", {"kind": "review", "day": day.isoformat(),
                                           "item_ids": [i["item_id"] for i in due], "index": 0, "results": []})
    cards = [_review_card(i, n + 1, len(due), reveal=False) for n, i in enumerate(due)]
    return {"status": "review_started", "cards": cards, "current": 0, "clock": clock_view(p)}


def _submit_review(lid: str, act: dict[str, Any], answer: str) -> dict[str, Any]:
    p = profile(lid)
    st = get_store()
    item_id = act["item_ids"][act["index"]]
    item = st.get(_pk(lid), f"review#{item_id}")
    g = grader.grade(answer=answer, task_en=item["prompt_en"], model_answers=item["accepted"],
                     model_ko=item["answer_ko"], model_romanization=item["answer_romanization"],
                     scene_setting=item.get("setting_en", ""), previous_line=item.get("previous_line"))
    day = today(p)
    item["reviews"] = item.get("reviews", 0) + 1
    if g["verdict"] == "correct":
        item["box"] = min(item["box"] + 1, len(LEITNER_DAYS) - 1)
        p["xp"] = p.get("xp", 0) + 5
    else:
        item["box"] = 0
        item["lapses"] = item.get("lapses", 0) + 1
    item["due_day"] = (day + timedelta(days=LEITNER_DAYS[item["box"]])).isoformat()
    item.update({"last_answer": answer, "last_verdict": g["verdict"], "last_corrected": g.get("corrected_ko", "")})
    st.put(_pk(lid), f"review#{item_id}", item)
    _save_profile(p)

    act["results"].append({"item_id": item_id, "verdict": g["verdict"]})
    act["index"] += 1
    total = len(act["item_ids"])
    graded_card = _review_card(item, act["index"], total, reveal=True)
    out = {"answer_ko": answer, "grade": g, "card": graded_card, "next_review_day": item["due_day"],
           "box": item["box"], "clock": clock_view(p)}
    if act["index"] >= total:
        st.delete(_pk(lid), "review_activity")
        out.update({"status": "review_complete",
                    "summary": {"reviewed": total,
                                "correct": sum(1 for r in act["results"] if r["verdict"] == "correct")}})
    else:
        st.put(_pk(lid), "review_activity", act)
        nxt = st.get(_pk(lid), f"review#{act['item_ids'][act['index']]}")
        out.update({"status": "next_card", "next_card": _review_card(nxt, act["index"] + 1, total, reveal=False)})
    return out


# ---------------------------------------------------------------- progress
def get_progress(lid: str) -> dict[str, Any]:
    p = profile(lid)
    st = get_store()
    day = today(p)
    days = st.query(_pk(lid), "day#")
    attempts = [a for d in days for a in d.get("attempts", [])]
    first: dict[tuple[str, str], dict[str, Any]] = {}
    for d in days:
        for a in d.get("attempts", []):
            first.setdefault((d["date"], a["line_id"]), a)
    scored = list(first.values())
    accuracy = round(100 * sum(1 for a in scored if a["verdict"] == "correct") / len(scored)) if scored else None
    items = st.query(_pk(lid), "review#")
    boxes = [0] * len(LEITNER_DAYS)
    for i in items:
        boxes[i["box"]] += 1
    week = []
    by_date = {d["date"]: d for d in days}
    for k in range(6, -1, -1):
        dd = day - timedelta(days=k)
        rec = by_date.get(dd.isoformat())
        week.append({"date": dd.isoformat(), "weekday": WEEKDAYS[dd.weekday()],
                     "status": rec["status"] if rec else "none", "is_today": k == 0})
    scenes = load_scenes()
    completed = sorted({d["scene_id"] for d in days if d["status"] == "completed"})
    streak = p.get("streak", 0)
    last = p.get("last_completed_day")
    if last and date.fromisoformat(last) < day - timedelta(days=1):
        streak = 0  # streak broken: no completed scene yesterday or today
    sched = st.get(_pk(lid), "schedule#practice")
    return {
        "clock": clock_view(p), "streak": streak, "best_streak": p.get("best_streak", 0), "xp": p.get("xp", 0),
        "days_practiced": len([d for d in days if d["status"] == "completed"]), "week": week,
        "accuracy_first_try": accuracy, "turns_answered": len(attempts),
        "scenes_completed": [{"scene_id": s, "title_en": scenes[s].data["title_en"],
                              "title_ko": scenes[s].data["title_ko"]} for s in completed if s in scenes],
        "review": {"total": len(items), "due_today": len(_due_reviews(lid, day)), "boxes": boxes,
                   "box_days": LEITNER_DAYS,
                   "weakest": [{"ko": i.get("focus_ko") or i["answer_ko"], "en": i.get("focus_en") or i["prompt_en"],
                                "lapses": i.get("lapses", 0)}
                               for i in sorted(items, key=lambda i: (-i.get("lapses", 0), i["box"]))[:3]]},
        "packs_owned": [_pack_view(get_pack(pid), lid) for pid in owned_packs(lid) if get_pack(pid)],
        "schedule": _schedule_view(p, sched) if sched else None,
    }


# ---------------------------------------------------------------- packs & purchase
def _pack_view(pack: dict[str, Any], lid: str) -> dict[str, Any]:
    scenes = load_scenes()
    return {"pack_id": pack["pack_id"], "title_en": pack["title_en"], "title_ko": pack["title_ko"],
            "tagline_en": pack["tagline_en"], "price_usd": pack["price_usd"], "accent": pack.get("accent"),
            "scene_count": len(pack["scene_ids"]),
            "scenes": [{"scene_id": s, "title_en": scenes[s].data["title_en"], "title_ko": scenes[s].data["title_ko"],
                        "available": True} if s in scenes else {"scene_id": s, "available": False}
                       for s in pack["scene_ids"]],
            "owned": pack["pack_id"] in owned_packs(lid)}


def browse_scene_packs(lid: str) -> dict[str, Any]:
    profile(lid)
    return {"packs": [_pack_view(pk, lid) for pk in load_catalog()["packs"]]}


def purchase_scene_pack(lid: str, pack: str, confirm: bool, purchase_token: str | None,
                        cancel: bool = False) -> dict[str, Any]:
    p = profile(lid)
    st = get_store()
    found = get_pack(pack) or find_pack(pack)
    if not found:
        names = ", ".join(f"{pk['title_en']} ({pk['pack_id']})" for pk in load_catalog()["packs"])
        raise FlowError(f"No pack matches '{pack}'. Packs: {names}.")
    view = _pack_view(found, lid)
    if view["owned"]:
        return {"status": "already_owned", "pack": view}
    if not found["available_scene_ids"]:
        raise FlowError(f"'{found['title_en']}' has no playable scenes yet, so it is not on sale. Offer another pack.")
    now = now_utc(p)

    if not confirm and not cancel:
        token = "pt_" + uuid.uuid4().hex[:12]
        st.put(_pk(lid), f"purchase#{token}", {
            "token": token, "pack_id": found["pack_id"], "status": "pending", "price_usd": found["price_usd"],
            "created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=PURCHASE_TTL_MIN)).isoformat()})
        return {"status": "awaiting_confirmation", "pack": view, "purchase_token": token,
                "price_usd": found["price_usd"], "payment_method": "Demo wallet (no real charge)",
                "expires_in_minutes": PURCHASE_TTL_MIN}

    if not purchase_token:
        raise FlowError("A purchase must be confirmed with the purchase_token from the confirmation step. "
                        "Call purchase_scene_pack without confirm first and ask the learner to confirm.")
    rec = st.get(_pk(lid), f"purchase#{purchase_token}")
    if not rec or rec["pack_id"] != found["pack_id"]:
        raise FlowError("That purchase_token does not match a pending purchase of this pack.")
    if rec["status"] != "pending":
        raise FlowError(f"This purchase is already {rec['status']}.")
    if cancel:
        rec.update({"status": "cancelled", "cancelled_at": now.isoformat()})
        st.put(_pk(lid), f"purchase#{purchase_token}", rec)
        return {"status": "cancelled", "pack": view}
    if now.isoformat() > rec["expires_at"]:
        rec["status"] = "expired"
        st.put(_pk(lid), f"purchase#{purchase_token}", rec)
        raise FlowError("The confirmation expired. Start the purchase again.")
    rec.update({"status": "confirmed", "confirmed_at": now.isoformat(),
                "order_id": "DKS-" + uuid.uuid4().hex[:8].upper()})
    st.put(_pk(lid), f"purchase#{purchase_token}", rec)
    return {"status": "purchased", "pack": _pack_view(found, lid), "order_id": rec["order_id"],
            "price_usd": rec["price_usd"], "payment_method": "Demo wallet (no real charge)"}


# ---------------------------------------------------------------- practice schedule
_DAY_ALIASES = {
    "weekdays": WEEKDAYS[:5], "weekday": WEEKDAYS[:5], "weekends": WEEKDAYS[5:], "weekend": WEEKDAYS[5:],
    "daily": WEEKDAYS, "everyday": WEEKDAYS, "every day": WEEKDAYS,
}


def _parse_days(days: list[str]) -> list[str]:
    out: list[str] = []
    for d in days:
        key = d.strip().lower()
        if key in _DAY_ALIASES:
            out += _DAY_ALIASES[key]
        elif key[:3] in WEEKDAYS:
            out.append(key[:3])
        else:
            raise FlowError(f"Unknown day '{d}'. Use mon..sun, 'weekdays', 'weekends' or 'daily'.")
    return [d for d in WEEKDAYS if d in out]


def _parse_time(text: str) -> time:
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*", text.lower())
    if not m:
        raise FlowError(f"Unrecognised time '{text}'. Use HH:MM (24h) like 07:30.")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if not (0 <= h < 24 and 0 <= mi < 60):
        raise FlowError(f"Unrecognised time '{text}'.")
    return time(h, mi)


def _next_occurrence(p: dict[str, Any], days: list[str], at: time, tz: str) -> datetime:
    zone = ZoneInfo(tz)
    local_now = now_utc(p).astimezone(zone)
    for k in range(0, 8):
        d = local_now.date() + timedelta(days=k)
        if WEEKDAYS[d.weekday()] in days:
            cand = datetime.combine(d, at, tzinfo=zone)
            if cand > local_now:
                return cand
    raise FlowError("Could not compute the next practice time.")


def _schedule_view(p: dict[str, Any], s: dict[str, Any]) -> dict[str, Any]:
    nxt = _next_occurrence(p, s["days"], _parse_time(s["time_local"]), s["timezone"])
    return {**s, "next_at_local": nxt.isoformat(timespec="minutes"),
            "next_at_utc": nxt.astimezone(timezone.utc).isoformat(timespec="minutes"),
            "next_label": nxt.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")}


def schedule_practice(lid: str, days: list[str], time_local: str, tz: str | None = None,
                      minutes: int = 5) -> dict[str, Any]:
    p = profile(lid)
    if tz:
        try:
            ZoneInfo(tz)
        except Exception:
            raise FlowError(f"Unknown timezone '{tz}'. Use an IANA name like America/New_York.")
        p["timezone"] = tz
        _save_profile(p)
    parsed_days = _parse_days(days)
    if not parsed_days:
        raise FlowError("Pick at least one day.")
    at = _parse_time(time_local)
    s = {"days": parsed_days, "time_local": at.strftime("%H:%M"), "timezone": p["timezone"],
         "minutes": max(1, min(int(minutes), 60)), "updated_at": now_utc(p).isoformat()}
    get_store().put(_pk(lid), "schedule#practice", s)
    return {"status": "scheduled", "schedule": _schedule_view(p, s), "clock": clock_view(p)}
