"""Helpers for cleaning and parsing local LLM output safely."""

from __future__ import annotations

import json
import re
from typing import Any

from src.core.llm.errors import LLMJSONError


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", flags=re.IGNORECASE)


def clean_llm_text(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = _CODE_FENCE_RE.sub("", cleaned).strip()
    cleaned = cleaned.replace("</think>", " ").strip()
    return cleaned


def extract_json_values(text: str) -> list[Any]:
    cleaned = clean_llm_text(text)
    decoder = json.JSONDecoder()
    matches: list[Any] = []

    for idx, char in enumerate(cleaned):
        if char not in "[{":
            continue
        try:
            parsed, _ = decoder.raw_decode(cleaned[idx:])
        except json.JSONDecodeError:
            continue
        matches.append(parsed)

    if not matches:
        raise LLMJSONError(
            f"LLM did not return valid JSON. Raw output: {cleaned[:1000]}",
            reason="validation_failed",
        )
    return matches


def extract_last_json_value(text: str) -> Any:
    return extract_json_values(text)[-1]


def extract_first_json_object(text: str) -> dict[str, Any]:
    for parsed in extract_json_values(text):
        if isinstance(parsed, dict):
            return parsed
    raise LLMJSONError(
        "LLM JSON response must include an object.",
        reason="validation_failed",
    )


def extract_last_json_object(text: str) -> dict[str, Any]:
    for parsed in reversed(extract_json_values(text)):
        if isinstance(parsed, dict):
            return parsed
    raise LLMJSONError(
        "LLM JSON response must be an object.",
        reason="validation_failed",
    )


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse one JSON-only object, allowing only an optional Markdown code fence."""
    cleaned = clean_llm_text(text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMJSONError(
            "LLM response must contain exactly one JSON object and no extra prose.",
            reason="validation_failed",
        ) from exc
    if not isinstance(parsed, dict):
        raise LLMJSONError(
            "LLM JSON response must be an object.",
            reason="validation_failed",
        )
    return parsed
