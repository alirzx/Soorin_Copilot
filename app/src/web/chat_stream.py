"""Streamlit-neutral helpers for consuming normalized Copilot SSE events."""

from __future__ import annotations

import codecs
import json
from collections.abc import Iterable, Iterator
from typing import Any


class ChatStreamProtocolError(ValueError):
    """Raised when the backend emits an invalid normalized SSE event."""


def parse_sse_events(lines: Iterable[str | bytes]) -> Iterator[dict[str, Any]]:
    """Parse SSE data records while ignoring comments and unknown fields."""
    data_lines: list[str] = []
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""

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

    def iter_complete_lines(fragment: str) -> Iterator[str]:
        nonlocal pending
        pending += fragment
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            yield line

    def handle_line(line: str) -> Iterator[dict[str, Any]]:
        line = line.rstrip("\r\n")
        if not line:
            event = decode_event()
            if event is not None:
                yield event
            return
        if line.startswith(":") or line.startswith("event:"):
            return
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    saw_byte_chunks = False
    for raw_line in lines:
        if isinstance(raw_line, bytes):
            saw_byte_chunks = True
            for line in iter_complete_lines(decoder.decode(raw_line)):
                yield from handle_line(line)
            continue

        for line in str(raw_line).splitlines() or [""]:
            yield from handle_line(line)

    if saw_byte_chunks:
        tail = pending + decoder.decode(b"", final=True)
        pending = ""
        if tail:
            yield from handle_line(tail)

    final_event = decode_event()
    if final_event is not None:
        yield final_event


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
