"""Soorin-owned persistence ports independent of concrete storage engines."""

from __future__ import annotations

from typing import Protocol

from src.core.identity import RequestIdentity
from src.core.memory.persistence import (
    ThreadMemoryState,
    LocalChatMessage,
    LocalConversation,
    LocalRequestCommit,
    LocalUser,
)
from src.core.memory.long_term import (
    EpistemicStatus,
    LongTermMemoryRecord,
    MemoryStatus,
    MemoryType,
)


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


class LongTermMemoryStore(Protocol):
    """Validated durable-memory boundary, separate from raw chat messages."""

    def get(self, *, user_id: str, memory_id: str) -> LongTermMemoryRecord | None: ...

    def put(self, *, memory: LongTermMemoryRecord) -> LongTermMemoryRecord: ...

    def update(
        self,
        *,
        memory: LongTermMemoryRecord,
        expected_revision: int,
    ) -> LongTermMemoryRecord: ...

    def list(
        self,
        *,
        user_id: str,
        entity_ids: tuple[str, ...] = (),
        memory_types: tuple[MemoryType, ...] = (),
        statuses: tuple[MemoryStatus, ...] = ("active",),
        epistemic_statuses: tuple[EpistemicStatus, ...] = (),
        limit: int = 100,
    ) -> tuple[LongTermMemoryRecord, ...]: ...

    def invalidate(
        self,
        *,
        user_id: str,
        memory_id: str,
        expected_revision: int,
    ) -> LongTermMemoryRecord: ...

    def supersede(
        self,
        *,
        user_id: str,
        memory_id: str,
        replacement: LongTermMemoryRecord,
        expected_revision: int,
    ) -> tuple[LongTermMemoryRecord, LongTermMemoryRecord]: ...

    def delete(self, *, user_id: str, memory_id: str) -> bool: ...


class SemanticMemoryIndex(Protocol):
    """Retrieval-only index boundary; canonical memory remains elsewhere."""

    def index(self, memory: LongTermMemoryRecord) -> None: ...

    def delete(self, memory_id: str) -> None: ...
