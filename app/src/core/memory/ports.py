"""Soorin-owned persistence ports independent of concrete storage engines."""

from __future__ import annotations

from typing import Protocol, TypeVar

from src.core.identity import RequestIdentity
from src.core.memory.persistence import (
    ThreadMemoryState,
    LocalChatMessage,
    LocalConversation,
    LocalRequestCommit,
    LocalUser,
)

MemoryT = TypeVar("MemoryT")
MatchT = TypeVar("MatchT")


class ChatRepository(Protocol):
    """Product-owned transcript boundary; SQLite is local simulation only."""

    def create_user(self, *, user_id: str | None = None) -> LocalUser: ...

    def list_users(self, *, limit: int = 50) -> tuple[LocalUser, ...]: ...

    def get_user(self, *, user_id: str) -> LocalUser | None: ...

    def create_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str,
        session_id: str | None = None,
        title: str = "",
    ) -> LocalConversation: ...

    def list_conversations(
        self,
        *,
        user_id: str,
        limit: int = 50,
    ) -> tuple[LocalConversation, ...]: ...

    def get_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> LocalConversation | None: ...

    def append(
        self,
        *,
        user_id: str,
        conversation_id: str,
        request_id: str,
        role: str,
        content: str,
        status: str = "completed",
    ) -> LocalChatMessage: ...

    def recent(
        self,
        *,
        user_id: str,
        conversation_id: str,
        limit: int = 50,
    ) -> tuple[LocalChatMessage, ...]: ...

    def delete_conversation(self, *, user_id: str, conversation_id: str) -> bool: ...

    def begin_request(
        self,
        *,
        user_id: str,
        conversation_id: str,
        request_id: str,
    ) -> LocalRequestCommit: ...

    def commit_turn(
        self,
        *,
        user_id: str,
        conversation_id: str,
        request_id: str,
        user_content: str,
        assistant_content: str,
    ) -> tuple[LocalChatMessage, LocalChatMessage]: ...

    def mark_request_status(
        self,
        *,
        user_id: str,
        conversation_id: str,
        request_id: str,
        status: str,
    ) -> LocalRequestCommit: ...

    def request_status(
        self,
        *,
        user_id: str,
        conversation_id: str,
        request_id: str,
    ) -> LocalRequestCommit | None: ...


class ThreadStateStore(Protocol):
    """Copilot-owned compact thread-state boundary."""

    def load(self, *, identity: RequestIdentity) -> ThreadMemoryState | None: ...

    def save(
        self,
        *,
        identity: RequestIdentity,
        state: ThreadMemoryState,
        expected_revision: int,
    ) -> ThreadMemoryState: ...

    def delete(self, *, identity: RequestIdentity) -> bool: ...


class LongTermMemoryStore(Protocol[MemoryT]):
    """Validated durable-memory boundary, separate from raw chat messages."""

    def get(self, *, memory_id: str) -> MemoryT | None: ...

    def put(self, *, memory: MemoryT) -> None: ...


class SemanticMemoryIndex(Protocol[MatchT]):
    """Retrieval-only index boundary; canonical memory remains elsewhere."""

    def search(self, *, query: str, limit: int) -> tuple[MatchT, ...]: ...
