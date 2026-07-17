"""Streamlit-neutral helpers for consuming normalized Copilot SSE events."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any


class ChatStreamProtocolError(ValueError):
    """Raised when the backend emits an invalid normalized SSE event."""


def parse_sse_events(lines: Iterable[str | bytes]) -> Iterator[dict[str, Any]]:
    """Parse SSE data records while ignoring comments and unknown fields."""
    data_lines: list[str] = []

    def decode_event() -> dict[str, Any] | None:
        if not data_lines:
            return None
        raw_data = "\n".join(data_lines)
        data_lines.clear()
        try:
            event = json.loads(raw_data)
        except (TypeError, ValueError) as exc:
            raise ChatStreamProtocolError("The backend returned an invalid stream event.") from exc
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ChatStreamProtocolError("The backend returned an invalid stream event.")
        return event

    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
        line = line.rstrip("\r\n")
        if not line:
            event = decode_event()
            if event is not None:
                yield event
            continue
        if line.startswith(":") or line.startswith("event:"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    event = decode_event()
    if event is not None:
        yield event


def collect_visible_stream(events: Iterable[dict[str, Any]]) -> tuple[str, str, bool, str]:
    """Collect separately rendered text for deterministic UI-focused tests."""
    reasoning_parts: list[str] = []
    answer_parts: list[str] = []
    completed = False
    error_message = ""
    for event in events:
        event_type = event.get("type")
        text = event.get("text")
        if event_type == "reasoning_delta" and isinstance(text, str):
            reasoning_parts.append(text)
        elif event_type == "answer_delta" and isinstance(text, str):
            answer_parts.append(text)
        elif event_type == "done":
            completed = True
        elif event_type == "error":
            error_message = str(event.get("message") or "The streamed response failed.")
    return "".join(reasoning_parts), "".join(answer_parts), completed, error_message
