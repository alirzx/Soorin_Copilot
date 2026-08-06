"""Typed local-development simulation API contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LocalSimulationStatus(BaseModel):
    status: Literal["not_enabled", "creation_disabled"]
    detail: str


class LocalUserResponse(BaseModel):
    user_id: str
    created_at: str


class LocalUsersResponse(BaseModel):
    status: Literal["ok"] = "ok"
    users: list[LocalUserResponse]


class LocalUserCreateRequest(BaseModel):
    """An optional opaque local identifier; no passwords or claims."""

    user_id: str | None = Field(default=None, max_length=128)


class LocalConversationCreateRequest(BaseModel):
    title: str = Field(default="", max_length=256)


class LocalConversationResponse(BaseModel):
    conversation_id: str
    user_id: str
    session_id: str
    title: str
    created_at: str
    updated_at: str


class LocalConversationsResponse(BaseModel):
    status: Literal["ok"] = "ok"
    conversations: list[LocalConversationResponse]


class LocalChatMessageResponse(BaseModel):
    message_id: str
    conversation_id: str
    request_id: str
    role: Literal["user", "assistant"]
    content: str
    status: Literal["completed", "interrupted", "failed"]
    position: int
    created_at: str


class LocalMessagesResponse(BaseModel):
    status: Literal["ok"] = "ok"
    messages: list[LocalChatMessageResponse]


class LocalDeleteResponse(BaseModel):
    status: Literal["ok"] = "ok"
    deleted: bool
