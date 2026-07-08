"""Baseline Copilot chat service."""

from __future__ import annotations

import logging
from uuid import uuid4

from src.config.settings import Settings
from src.core.llm.client import LLMClient
from src.core.memory.store import MemoryStore


logger = logging.getLogger(__name__)


def _preview(text: str) -> str:
    return text.strip().replace("\n", " ")[:120]


class CopilotService:
    def __init__(self, settings: Settings, llm_client: LLMClient, memory_store: MemoryStore) -> None:
        self.settings = settings
        self.llm_client = llm_client
        self.memory_store = memory_store

    def chat(self, message: str, session_id: str | None = None) -> dict[str, str]:
        session = (session_id or "").strip() or uuid4().hex
        user_text = message.strip()

        history = self.memory_store.get(session) if self.settings.chat_store_history else []
        messages = [*history, {"role": "user", "content": user_text}]
        logger.info(
            "event=conversation_request session_id=%s message_count=%s user_preview=%r",
            session,
            len(messages),
            _preview(user_text),
        )
        result = self.llm_client.chat(messages)
        logger.info(
            "event=conversation_response session_id=%s provider=%s model=%s assistant_preview=%r",
            session,
            result.provider,
            result.model,
            _preview(result.text),
        )

        if self.settings.chat_store_history:
            self.memory_store.append(session, "user", user_text)
            self.memory_store.append(session, "assistant", result.text)

        return {
            "session_id": session,
            "answer": result.text,
            "provider": result.provider,
            "model": result.model,
        }
