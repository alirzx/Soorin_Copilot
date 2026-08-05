"""Dormant Soorin-owned persistence ports for future memory adapters."""

from __future__ import annotations

from typing import Protocol, TypeVar


MessageT = TypeVar("MessageT")
ThreadStateT = TypeVar("ThreadStateT")
MemoryT = TypeVar("MemoryT")
MatchT = TypeVar("MatchT")


class ChatRepository(Protocol[MessageT]):
    """Product-owned transcript boundary; no adapter is active in Gate 2."""

    def append(self, *, conversation_id: str, message: MessageT) -> None: ...

    def recent(self, *, conversation_id: str, limit: int) -> tuple[MessageT, ...]: ...


class ThreadStateStore(Protocol[ThreadStateT]):
    """Copilot-owned compact thread-state boundary."""

    def load(self, *, thread_key: str) -> ThreadStateT | None: ...

    def save(self, *, thread_key: str, state: ThreadStateT) -> None: ...


class LongTermMemoryStore(Protocol[MemoryT]):
    """Validated durable-memory boundary, separate from raw chat messages."""

    def get(self, *, memory_id: str) -> MemoryT | None: ...

    def put(self, *, memory: MemoryT) -> None: ...


class SemanticMemoryIndex(Protocol[MatchT]):
    """Retrieval-only index boundary; canonical memory remains elsewhere."""

    def search(self, *, query: str, limit: int) -> tuple[MatchT, ...]: ...
