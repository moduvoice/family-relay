"""Grade a learner's Korean answer: exact match against the model answers first, the LLM otherwise."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from . import config
from .llm import get_provider

PROMPT = (config.APP_DIR / "prompts" / "grade.md").read_text(encoding="utf-8")

GRADE_TOOL = {
    "name": "report_grade",
    "description": "Report the grade for the learner's answer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["correct", "close", "incorrect"]},
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["grammar", "vocabulary", "politeness", "meaning"]},
                        "learner_span": {"type": "string"},
                        "fix_span": {"type": "string"},
                        "explanation_en": {"type": "string"},
                    },
                    "required": ["type", "learner_span", "fix_span", "explanation_en"],
                },
            },
            "corrected_ko": {"type": "string"},
            "corrected_romanization": {"type": "string"},
            "corrected_en": {"type": "string"},
            "feedback_en": {"type": "string"},
            "focus_expression_ko": {"type": "string"},
        },
        "required": ["verdict", "score", "issues", "corrected_ko", "corrected_romanization",
                     "corrected_en", "feedback_en", "focus_expression_ko"],
    },
}

_PUNCT = re.compile(r"[\s.,!?~…·'\"“”‘’。、]+")


def normalize(text: str) -> str:
    return _PUNCT.sub("", unicodedata.normalize("NFC", text or "")).strip()


def grade(*, answer: str, task_en: str, model_answers: list[str], model_ko: str, model_romanization: str,
          scene_setting: str, previous_line: dict[str, str] | None) -> dict[str, Any]:
    answer = (answer or "").strip()
    norm = normalize(answer)
    for m in model_answers:
        if norm and norm == normalize(m):
            same_as_model = norm == normalize(model_ko)
            return {
                "verdict": "correct", "score": 100, "issues": [],
                "corrected_ko": model_ko if same_as_model else m,
                "corrected_romanization": model_romanization if same_as_model else "",
                "corrected_en": task_en, "feedback_en": "Perfect - that's exactly how a local would say it.",
                "focus_expression_ko": "", "graded_by": "exact-match",
            }
    payload = {
        "scene": scene_setting,
        "previous_line": previous_line,
        "task_en": task_en,
        "model_answers": model_answers,
        "learner_answer": answer,
    }
    reply = get_provider().converse(
        model=config.GRADER_MODEL,
        system=PROMPT,
        messages=[{"role": "user", "content": [{"text": json.dumps(payload, ensure_ascii=False)}]}],
        tools=[GRADE_TOOL],
        force_tool="report_grade",
        max_tokens=800,
        temperature=0,
    )
    if not reply.tool_uses:
        raise RuntimeError(f"grader returned no report_grade call (stop={reply.stop_reason}): {reply.text[:200]}")
    result = dict(reply.tool_uses[0]["input"])
    # spans the card cannot find are dropped from highlighting, never invented
    for issue in result.get("issues", []):
        issue["learner_span_found"] = bool(issue.get("learner_span")) and issue["learner_span"] in answer
        issue["fix_span_found"] = bool(issue.get("fix_span")) and issue["fix_span"] in result.get("corrected_ko", "")
    result["graded_by"] = reply.model
    result["grader_latency_ms"] = reply.latency_ms
    return result
