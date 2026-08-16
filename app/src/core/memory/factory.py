"""Disabled-by-default construction for local persistence adapters."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.config.settings import Settings
from src.core.memory.ports import ChatRepository, LongTermMemoryStore, ThreadStateStore
from src.core.memory.persistence import LocalPersistenceError, MemoryStoragePolicy
from src.core.memory.sqlite import (
    LocalSQLiteDatabase,
    SQLiteChatRepository,
    SQLiteThreadStateStore,
)
from src.core.memory.sqlite_long_term import SQLiteLongTermMemoryStore


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalPersistenceAdapters:
    chat_repository: ChatRepository | None = None
    thread_state_store: ThreadStateStore | None = None
    long_term_memory_store: LongTermMemoryStore | None = None


def build_local_persistence(settings: Settings) -> LocalPersistenceAdapters:
    """Construct only explicitly enabled local adapters; failure is non-fatal."""
    chat_enabled = settings.local_product_simulation_enabled
    thread_enabled = settings.thread_state_backend == "sqlite"
    long_term_enabled = (
        bool(getattr(settings, "long_term_memory_enabled", False))
        and getattr(settings, "long_term_memory_backend", "sqlite") == "sqlite"
    )
    if not chat_enabled and not thread_enabled and not long_term_enabled:
        return LocalPersistenceAdapters()

    try:
        database = LocalSQLiteDatabase(settings.local_sqlite_path)
        database.initialize()
    except LocalPersistenceError as exc:
        logger.warning(
            "event=local_persistence_unavailable chat_enabled=%s thread_enabled=%s long_term_enabled=%s error_type=%s",
            chat_enabled,
            thread_enabled,
            long_term_enabled,
            type(exc).__name__,
        )
        return LocalPersistenceAdapters()

    logger.info(
        "event=local_persistence_ready chat_enabled=%s thread_enabled=%s long_term_enabled=%s schema=sqlite",
        chat_enabled,
        thread_enabled,
        long_term_enabled,
    )
    policy = MemoryStoragePolicy(
        max_conversations_per_user=settings.local_max_conversations_per_user,
        max_messages_per_conversation=settings.local_max_messages_per_conversation,
        max_active_long_term_per_user=settings.memory_max_active_records_per_user,
        max_candidate_long_term_per_user=settings.memory_max_candidate_records_per_user,
    )
    return LocalPersistenceAdapters(
        chat_repository=SQLiteChatRepository(database, policy) if chat_enabled else None,
        thread_state_store=SQLiteThreadStateStore(database) if thread_enabled else None,
        long_term_memory_store=(
            SQLiteLongTermMemoryStore(database, policy) if long_term_enabled else None
        ),
    )
