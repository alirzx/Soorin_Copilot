"""Protected local-only chatroom simulation routes backed by SQLite."""

from __future__ import annotations

import hashlib
import logging
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from src.api.auth import verify_api_key
from src.api.dependencies import get_local_persistence
from src.api.schemas.local_simulation import (
    LocalChatMessageResponse,
    LocalConversationCreateRequest,
    LocalConversationResponse,
    LocalConversationsResponse,
    LocalDeleteResponse,
    LocalMessagesResponse,
    LocalLoginRequest,
    LocalSimulationStatus,
    LocalUserCreateRequest,
    LocalUserResponse,
    LocalUsersResponse,
)
from src.config.settings import get_settings
from src.core.identity import normalize_identifier
from src.core.memory.factory import LocalPersistenceAdapters
from src.core.memory.persistence import LocalPersistenceError, LocalPersistenceOwnershipError
from src.core.memory.persistence import LocalPersistenceConflictError
from src.core.memory.local_auth import hash_password, normalize_username, verify_password
from src.core.memory.ports import ChatRepository


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/local-simulation", tags=["local simulation"])
UserIdHeader = Annotated[str | None, Header(alias="X-User-ID")]


def _reference(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _repository() -> ChatRepository | LocalSimulationStatus:
    if not get_settings().local_product_simulation_enabled:
        return LocalSimulationStatus(
            status="not_enabled",
            detail="Local development simulation is not enabled.",
        )
    adapters: LocalPersistenceAdapters = get_local_persistence()
    if adapters.chat_repository is None:
        raise HTTPException(status_code=503, detail="Local simulation storage is unavailable.")
    return adapters.chat_repository


def _required_user_id(value: str | None) -> str:
    if value is None:
        raise HTTPException(status_code=422, detail="X-User-ID is required for local conversations.")
    try:
        return normalize_identifier(value, field_name="X-User-ID") or ""
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid X-User-ID.") from exc


def _user_response(value) -> LocalUserResponse:
    return LocalUserResponse(
        user_id=value.user_id,
        username=value.username,
        created_at=value.created_at,
    )


def _conversation_response(value) -> LocalConversationResponse:
    return LocalConversationResponse(
        conversation_id=value.conversation_id,
        user_id=value.user_id,
        session_id=value.session_id,
        title=value.title,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _message_response(value) -> LocalChatMessageResponse:
    return LocalChatMessageResponse(**value.__dict__)


@router.get(
    "/users",
    response_model=LocalUsersResponse | LocalSimulationStatus,
    summary="List local simulation users",
    description="Local development only. Never a production identity endpoint.",
)
def list_users(_auth: None = Depends(verify_api_key)) -> LocalUsersResponse | LocalSimulationStatus:
    repository = _repository()
    if isinstance(repository, LocalSimulationStatus):
        return repository
    return LocalUsersResponse(users=[_user_response(item) for item in repository.list_users()])


@router.post(
    "/users",
    response_model=LocalUserResponse | LocalSimulationStatus,
    summary="Create a local simulation user",
    description="Local development only. Creates a password-hashed local identity, never a Product account.",
)
def create_user(
    request: LocalUserCreateRequest,
    _auth: None = Depends(verify_api_key),
) -> LocalUserResponse | LocalSimulationStatus:
    repository = _repository()
    if isinstance(repository, LocalSimulationStatus):
        return repository
    if not get_settings().local_test_user_creation_enabled:
        return LocalSimulationStatus(
            status="creation_disabled",
            detail="Local test-user creation is disabled.",
        )
    if request.password != request.confirm_password:
        raise HTTPException(status_code=422, detail="Passwords do not match.")
    try:
        username = normalize_username(request.username)
        user = repository.create_user(
            username=username,
            password_hash=hash_password(request.password),
        )
    except LocalPersistenceConflictError as exc:
        raise HTTPException(status_code=409, detail="Username is already registered.") from exc
    except (LocalPersistenceError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid local user.") from exc
    logger.info("event=local_user_created user_ref=%s", _reference(user.user_id))
    return _user_response(user)


@router.post(
    "/login",
    response_model=LocalUserResponse | LocalSimulationStatus,
    summary="Authenticate a local simulation user",
    description="Local development only. Does not authenticate a Product account.",
)
def login(
    request: LocalLoginRequest,
    _auth: None = Depends(verify_api_key),
) -> LocalUserResponse | LocalSimulationStatus:
    repository = _repository()
    if isinstance(repository, LocalSimulationStatus):
        return repository
    try:
        user = repository.get_user_by_username(username=normalize_username(request.username))
        encoded = repository.get_password_hash(user_id=user.user_id) if user else None
    except (LocalPersistenceError, ValueError):
        user = None
        encoded = None
    if user is None or encoded is None or not verify_password(request.password, encoded):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    logger.info("event=local_user_authenticated user_ref=%s", _reference(user.user_id))
    return _user_response(user)


@router.get(
    "/users/{user_id}",
    response_model=LocalUserResponse | LocalSimulationStatus,
    summary="Get one local simulation user",
    description="Local development only.",
)
def get_user(user_id: str, _auth: None = Depends(verify_api_key)) -> LocalUserResponse | LocalSimulationStatus:
    repository = _repository()
    if isinstance(repository, LocalSimulationStatus):
        return repository
    try:
        user = repository.get_user(user_id=user_id)
    except LocalPersistenceError as exc:
        raise HTTPException(status_code=422, detail="Invalid local user.") from exc
    if user is None:
        raise HTTPException(status_code=404, detail="Local user was not found.")
    return _user_response(user)


@router.get(
    "/conversations",
    response_model=LocalConversationsResponse | LocalSimulationStatus,
    summary="List the current local user's conversations",
    description="Local development only. X-User-ID scopes ownership.",
)
def list_conversations(
    x_user_id: UserIdHeader = None,
    _auth: None = Depends(verify_api_key),
) -> LocalConversationsResponse | LocalSimulationStatus:
    repository = _repository()
    if isinstance(repository, LocalSimulationStatus):
        return repository
    user_id = _required_user_id(x_user_id)
    return LocalConversationsResponse(
        conversations=[_conversation_response(item) for item in repository.list_conversations(user_id=user_id)]
    )


@router.post(
    "/conversations",
    response_model=LocalConversationResponse | LocalSimulationStatus,
    summary="Create a local conversation",
    description="Local development only. The returned session_id is stable for this conversation.",
)
def create_conversation(
    request: LocalConversationCreateRequest,
    x_user_id: UserIdHeader = None,
    _auth: None = Depends(verify_api_key),
) -> LocalConversationResponse | LocalSimulationStatus:
    repository = _repository()
    if isinstance(repository, LocalSimulationStatus):
        return repository
    user_id = _required_user_id(x_user_id)
    conversation = repository.create_conversation(
        user_id=user_id,
        conversation_id=f"local-chat-{uuid4().hex}",
        title=request.title,
    )
    logger.info(
        "event=local_conversation_created user_ref=%s conversation_ref=%s",
        _reference(user_id),
        _reference(conversation.conversation_id),
    )
    return _conversation_response(conversation)


@router.get(
    "/conversations/{conversation_id}",
    response_model=LocalConversationResponse | LocalSimulationStatus,
    summary="Open an owned local conversation",
    description="Local development only. X-User-ID scopes ownership.",
)
def get_conversation(
    conversation_id: str,
    x_user_id: UserIdHeader = None,
    _auth: None = Depends(verify_api_key),
) -> LocalConversationResponse | LocalSimulationStatus:
    repository = _repository()
    if isinstance(repository, LocalSimulationStatus):
        return repository
    user_id = _required_user_id(x_user_id)
    try:
        conversation = repository.get_conversation(user_id=user_id, conversation_id=conversation_id)
    except LocalPersistenceOwnershipError as exc:
        raise HTTPException(status_code=404, detail="Local conversation was not found.") from exc
    except LocalPersistenceError as exc:
        raise HTTPException(status_code=422, detail="Invalid local conversation.") from exc
    if conversation is None:
        raise HTTPException(status_code=404, detail="Local conversation was not found.")
    logger.info(
        "event=local_conversation_opened user_ref=%s conversation_ref=%s",
        _reference(user_id),
        _reference(conversation_id),
    )
    return _conversation_response(conversation)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=LocalMessagesResponse | LocalSimulationStatus,
    summary="Read ordered local conversation messages",
    description="Local development only. Messages are written only by completed /chat/stream turns.",
)
def get_messages(
    conversation_id: str,
    limit: int = Query(default=100, ge=1, le=200),
    x_user_id: UserIdHeader = None,
    _auth: None = Depends(verify_api_key),
) -> LocalMessagesResponse | LocalSimulationStatus:
    repository = _repository()
    if isinstance(repository, LocalSimulationStatus):
        return repository
    user_id = _required_user_id(x_user_id)
    try:
        messages = repository.recent(user_id=user_id, conversation_id=conversation_id, limit=limit)
    except LocalPersistenceOwnershipError as exc:
        raise HTTPException(status_code=404, detail="Local conversation was not found.") from exc
    except LocalPersistenceError as exc:
        raise HTTPException(status_code=422, detail="Invalid local conversation.") from exc
    return LocalMessagesResponse(messages=[_message_response(item) for item in messages])


@router.delete(
    "/conversations/{conversation_id}",
    response_model=LocalDeleteResponse | LocalSimulationStatus,
    summary="Delete an owned local conversation",
    description="Local development only. Deletes this conversation's local transcript and request records.",
)
def delete_conversation(
    conversation_id: str,
    x_user_id: UserIdHeader = None,
    _auth: None = Depends(verify_api_key),
) -> LocalDeleteResponse | LocalSimulationStatus:
    repository = _repository()
    if isinstance(repository, LocalSimulationStatus):
        return repository
    user_id = _required_user_id(x_user_id)
    try:
        deleted = repository.delete_conversation(user_id=user_id, conversation_id=conversation_id)
    except LocalPersistenceOwnershipError as exc:
        raise HTTPException(status_code=404, detail="Local conversation was not found.") from exc
    except LocalPersistenceError as exc:
        raise HTTPException(status_code=422, detail="Invalid local conversation.") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Local conversation was not found.")
    logger.info(
        "event=local_conversation_deleted user_ref=%s conversation_ref=%s",
        _reference(user_id),
        _reference(conversation_id),
    )
    return LocalDeleteResponse(deleted=True)
