"""Read-only Product chat adapter for bounded Copilot continuity restoration."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from src.core.identity import normalize_identifier
from src.core.memory.persistence import (
    MAX_CHAT_CONTENT_CHARS,
    MAX_CHAT_READ_LIMIT,
    LocalChatMessage,
    LocalPersistenceError,
    LocalPersistenceOwnershipError,
)
from src.core.product_client import ProductApiClient


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        return value["data"]
    return value


class ProductTranscriptRepository:
    """Fetch Product-owned chat history without writing transcript rows."""

    def __init__(self, client: ProductApiClient) -> None:
        self.client = client

    def recent(
        self,
        *,
        user_id: str,
        conversation_id: str,
        limit: int = 50,
        request_id: str = "",
    ) -> tuple[LocalChatMessage, ...]:
        try:
            owner = normalize_identifier(user_id, field_name="user_id")
            conversation = normalize_identifier(conversation_id, field_name="conversation_id")
        except ValueError as exc:
            raise LocalPersistenceOwnershipError("Product transcript identity was invalid.") from exc
        if owner is None or conversation is None:
            raise LocalPersistenceOwnershipError(
                "Product transcript requires user and conversation identity."
            )
        bounded = max(1, min(int(limit), MAX_CHAT_READ_LIMIT))
        base = self.client.settings.product_chat_rooms_path.rstrip("/")
        payload, _status, _elapsed = self.client.get_json(
            f"{base}/{quote(conversation, safe='')}",
            request_id=request_id,
            operation="chat_transcript",
            extra_headers={"X-User-ID": owner},
        )
        room = _unwrap(payload)
        if not isinstance(room, dict):
            raise LocalPersistenceError("Product transcript response was invalid.")
        returned_room = str(room.get("roomId") or room.get("id") or "").strip()
        if returned_room != conversation:
            raise LocalPersistenceOwnershipError(
                "Product transcript conversation did not match the request."
            )
        returned_owner = str(room.get("userId") or room.get("user_id") or "").strip()
        if returned_owner and returned_owner != owner:
            raise LocalPersistenceOwnershipError(
                "Product transcript owner did not match the request."
            )
        raw_messages = room.get("messages") or ()
        if not isinstance(raw_messages, (list, tuple)):
            raise LocalPersistenceError("Product transcript messages were invalid.")
        messages: list[LocalChatMessage] = []
        for position, item in enumerate(raw_messages):
            if not isinstance(item, dict):
                raise LocalPersistenceError("Product transcript message was invalid.")
            role = str(item.get("role") or "").strip().lower()
            content = str(item.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                raise LocalPersistenceError("Product transcript message was invalid.")
            if len(content) > MAX_CHAT_CONTENT_CHARS:
                raise LocalPersistenceError("Product transcript message exceeded its bound.")
            status = str(item.get("status") or "completed").strip().lower()
            if status != "completed":
                continue
            messages.append(
                LocalChatMessage(
                    message_id=str(item.get("messageId") or item.get("id") or position),
                    conversation_id=conversation,
                    request_id=str(item.get("requestId") or item.get("request_id") or ""),
                    role=role,
                    content=content,
                    status=status,
                    position=position,
                    created_at=str(item.get("createdAt") or item.get("created_at") or ""),
                )
            )
        return tuple(messages[-bounded:])
