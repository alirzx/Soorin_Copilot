"""Validated request identity shared across API and workflow boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import uuid4


IDENTIFIER_MAX_LENGTH = 128
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$"
_IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)


def normalize_identifier(value: str | None, *, field_name: str) -> str | None:
    """Validate one caller-controlled opaque identifier without interpreting it."""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    if len(normalized) > IDENTIFIER_MAX_LENGTH:
        raise ValueError(
            f"{field_name} must contain at most {IDENTIFIER_MAX_LENGTH} characters"
        )
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} contains unsupported characters")
    return normalized


@dataclass(frozen=True)
class RequestIdentity:
    """Per-request identity metadata; it does not grant authorization."""

    user_id: str | None
    conversation_id: str | None
    session_id: str
    request_id: str
    thread_key: str

    @classmethod
    def resolve(
        cls,
        *,
        user_id: str | None = None,
        conversation_id: str | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> "RequestIdentity":
        resolved_user_id = normalize_identifier(user_id, field_name="user_id")
        resolved_conversation_id = normalize_identifier(
            conversation_id,
            field_name="conversation_id",
        )
        resolved_session_id = normalize_identifier(
            session_id,
            field_name="session_id",
        ) or uuid4().hex
        resolved_request_id = normalize_identifier(
            request_id,
            field_name="request_id",
        ) or uuid4().hex[:12]
        return cls(
            user_id=resolved_user_id,
            conversation_id=resolved_conversation_id,
            session_id=resolved_session_id,
            request_id=resolved_request_id,
            thread_key=resolved_conversation_id or resolved_session_id,
        )
