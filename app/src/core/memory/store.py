"""Simple in-memory conversation store."""

from __future__ import annotations


class MemoryStore:
    def __init__(self, max_messages: int) -> None:
        self.max_messages = max(0, max_messages)
        self._history: dict[str, list[dict[str, str]]] = {}

    def get(self, session_id: str) -> list[dict[str, str]]:
        return list(self._history.get(session_id, []))

    def append(self, session_id: str, role: str, content: str) -> None:
        if self.max_messages == 0:
            return
        history = self._history.setdefault(session_id, [])
        history.append({"role": role, "content": content})
        self._history[session_id] = history[-self.max_messages :]
