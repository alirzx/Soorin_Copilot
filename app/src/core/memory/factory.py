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
from src.core.memory.product import ProductLongTermMemoryStore, ProductThreadStateStore
from src.core.product_client.memory_client import ProductMemoryClient


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalPersistenceAdapters:
    chat_repository: ChatRepository | None = None
    thread_state_store: ThreadStateStore | None = None
    long_term_memory_store: LongTermMemoryStore | None = None


def build_local_persistence(
    settings: Settings,
    *,
    product_memory_client: ProductMemoryClient | None = None,
) -> LocalPersistenceAdapters:
    """Construct only explicitly enabled local adapters; failure is non-fatal."""
    chat_enabled = settings.local_product_simulation_enabled
    thread_enabled = settings.thread_state_backend == "sqlite"
    product_thread_enabled = settings.thread_state_backend == "product"
    long_term_enabled = (
        bool(getattr(settings, "long_term_memory_enabled", False))
        and getattr(settings, "long_term_memory_backend", "sqlite") == "sqlite"
    )
    product_ltm_enabled = (
        bool(getattr(settings, "long_term_memory_enabled", False))
        and getattr(settings, "long_term_memory_backend", "sqlite") == "product"
    )
    if not chat_enabled and not thread_enabled and not product_thread_enabled and not long_term_enabled and not product_ltm_enabled:
        return LocalPersistenceAdapters()

    if (product_thread_enabled or product_ltm_enabled) and product_memory_client is None:
        logger.warning("event=product_memory_persistence_unavailable reason=client_missing")
        return LocalPersistenceAdapters()

    sqlite_required = chat_enabled or thread_enabled or long_term_enabled
    if not sqlite_required:
        return LocalPersistenceAdapters(
            thread_state_store=ProductThreadStateStore(
                product_memory_client,
                local_test_user_id="",
            ) if product_thread_enabled and product_memory_client is not None else None,
            long_term_memory_store=ProductLongTermMemoryStore(
                product_memory_client,
                local_test_user_id=(settings.local_product_test_user_id if settings.local_product_simulation_enabled else ""),
                require_local_test_user=settings.local_product_simulation_enabled,
            ) if product_ltm_enabled and product_memory_client is not None else None,
        )

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
        thread_state_store=(
            ProductThreadStateStore(
                product_memory_client,
                local_test_user_id=(
                    settings.local_product_test_user_id
                    if settings.local_product_simulation_enabled else ""
                ),
                require_local_test_user=settings.local_product_simulation_enabled,
            )
            if product_thread_enabled and product_memory_client is not None
            else SQLiteThreadStateStore(database) if thread_enabled else None
        ),
        long_term_memory_store=(
            SQLiteLongTermMemoryStore(database, policy) if long_term_enabled
            else ProductLongTermMemoryStore(
                product_memory_client,
                local_test_user_id=(settings.local_product_test_user_id if settings.local_product_simulation_enabled else ""),
                require_local_test_user=settings.local_product_simulation_enabled,
            ) if product_ltm_enabled and product_memory_client is not None else None
        ),
    )
