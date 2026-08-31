"""Thin Product-backed controller for the unified Streamlit workspace."""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Iterator, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import streamlit as st

from src.config.settings import Settings
from src.web.chat_backend import ConversationController
from src.web.chat_stream import ChatStreamProtocolError, parse_sse_events
from src.web.chat_ui import render_conversation_chat
from src.web.local_simulation import LocalConversation
from src.web.pages.topology import build_copilot_ui_context, show_topology_page
from src.web.product_user_chat import (
    ProductChatRoom,
    ProductLoginSession,
    ProductUserChatClient,
    ProductUserChatError,
    ProductUserChatHTTPError,
)


logger = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parents[2]
LOGO_PATH = APP_DIR / "assets" / "branding" / "soorinsec-logo2.png"

PRODUCT_TOKEN_KEY = "product_access_token"
PRODUCT_USER_ID_KEY = "product_user_id"
PRODUCT_USERNAME_KEY = "product_username"
PRODUCT_DISPLAY_NAME_KEY = "product_display_name"
PRODUCT_ROLE_KEY = "product_role"
PRODUCT_ROOM_KEY = "product_room_id"
PRODUCT_DELETE_CONFIRMATION_KEY = "product_delete_confirmation"
PRODUCT_STREAMING_KEY = "product_streaming"
PRODUCT_TURN_KEY = "product_turn_state"
PRODUCT_STATE_KEYS = (
    PRODUCT_TOKEN_KEY,
    PRODUCT_USER_ID_KEY,
    PRODUCT_USERNAME_KEY,
    PRODUCT_DISPLAY_NAME_KEY,
    PRODUCT_ROLE_KEY,
    PRODUCT_ROOM_KEY,
    PRODUCT_DELETE_CONFIRMATION_KEY,
    PRODUCT_STREAMING_KEY,
    PRODUCT_TURN_KEY,
)


class ProductCopilotStreamError(RuntimeError):
    """Safe Product-mode Copilot streaming error."""


def clear_product_ui_state(state: MutableMapping[str, Any]) -> None:
    for key in PRODUCT_STATE_KEYS:
        state.pop(key, None)


def _message_fingerprint(room_id: str, content: str) -> str:
    return hashlib.sha256(f"{room_id}\0{content}".encode("utf-8")).hexdigest()


def _conversation(room: ProductChatRoom, user_id: str) -> LocalConversation:
    return LocalConversation(
        conversation_id=room.room_id,
        user_id=user_id,
        session_id=room.room_id,
        title=room.title,
        created_at=room.created_at,
        updated_at=room.updated_at,
    )


@dataclass
class ProductConversationBackend:
    settings: Settings
    client: ProductUserChatClient
    state: MutableMapping[str, Any]
    http: requests.Session | None = None
    reauthenticate: Callable[[], ProductLoginSession | None] | None = None

    def current_user(self) -> str | None:
        value = str(self.state.get(PRODUCT_USER_ID_KEY) or "").strip()
        return value or None

    def login(self, user_id: str) -> None:
        if not user_id:
            raise ProductUserChatError("Product user identity is required.")
        self.state[PRODUCT_USER_ID_KEY] = user_id

    def logout(self) -> None:
        clear_product_ui_state(self.state)

    def _user(self) -> str:
        user_id = self.current_user()
        if not user_id:
            raise ProductUserChatError("Product user authentication is required.")
        return user_id

    def _reauthenticate(self) -> ProductLoginSession | None:
        if self.reauthenticate is not None:
            return self.reauthenticate()
        username = str(self.state.get(PRODUCT_USERNAME_KEY) or "").strip()
        configured_username = str(
            getattr(self.settings, "product_username", "") or ""
        ).strip()
        configured_password = str(
            getattr(self.settings, "product_password", "") or ""
        )
        if not username or username != configured_username or not configured_password:
            return None
        return ProductUserChatClient(self.settings).login(
            username,
            configured_password,
        )

    def _apply_reauthenticated_session(self, session: ProductLoginSession) -> bool:
        if session.user_id != self._user():
            return False
        self.client.token = session.access_token
        self.state[PRODUCT_TOKEN_KEY] = session.access_token
        self.state[PRODUCT_USERNAME_KEY] = (
            session.username or self.state.get(PRODUCT_USERNAME_KEY, "")
        )
        self.state[PRODUCT_DISPLAY_NAME_KEY] = (
            session.display_name or self.state.get(PRODUCT_DISPLAY_NAME_KEY, "")
        )
        self.state[PRODUCT_ROLE_KEY] = (
            session.role or self.state.get(PRODUCT_ROLE_KEY, "")
        )
        logger.info("event=product_ui_session_reauthenticated user_ref_set=true")
        return True

    def _product_call(self, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except ProductUserChatHTTPError as exc:
            if exc.status_code != 401:
                raise
            try:
                session = self._reauthenticate()
            except ProductUserChatHTTPError as reauth_exc:
                if reauth_exc.status_code in {401, 403}:
                    self.logout()
                raise
            except ProductUserChatError:
                raise
            if session is None or not self._apply_reauthenticated_session(session):
                self.logout()
                raise exc
            try:
                return operation()
            except ProductUserChatHTTPError as retry_exc:
                if retry_exc.status_code == 401:
                    self.logout()
                raise

    def list_conversations(self) -> list[LocalConversation]:
        rooms = self._product_call(self.client.list_rooms)
        return [_conversation(room, self._user()) for room in rooms]

    def create_conversation(self, title: str = "") -> LocalConversation:
        selected_ip = str(self.state.get("selected_copilot_ip") or "").strip()
        room = self._product_call(
            lambda: self.client.create_room(title or "Asset investigation", selected_ip)
        )
        return _conversation(room, self._user())

    def get_conversation(self, conversation_id: str) -> LocalConversation:
        room = self._product_call(lambda: self.client.get_room(conversation_id))
        return _conversation(room, self._user())

    def get_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        room = self._product_call(lambda: self.client.get_room(conversation_id))
        return [{"role": item.role, "content": item.content} for item in room.messages]

    def delete_conversation(self, conversation_id: str) -> None:
        self._product_call(lambda: self.client.delete_room(conversation_id))

    def _turn(
        self,
        conversation: LocalConversation,
        *,
        request_id: str,
        message: str,
    ) -> dict[str, Any]:
        fingerprint = _message_fingerprint(conversation.conversation_id, message)
        current = self.state.get(PRODUCT_TURN_KEY)
        if isinstance(current, dict) and current.get("request_id") == request_id:
            if current.get("room_id") != conversation.conversation_id or current.get("message_fingerprint") != fingerprint:
                raise ProductCopilotStreamError("Product turn identity changed during processing.")
            return current
        if isinstance(current, dict) and not current.get("completed"):
            raise ProductCopilotStreamError(
                "The previous Product turn must be resolved before starting another."
            )
        turn = {
            "request_id": request_id,
            "room_id": conversation.conversation_id,
            "message_fingerprint": fingerprint,
            "user_persisted": False,
            "stream_started": False,
            "stream_done": False,
            "assistant_persisted": False,
            "completed": False,
            "assistant_content": "",
        }
        self.state[PRODUCT_TURN_KEY] = turn
        return turn

    def stream_chat(
        self,
        conversation: LocalConversation,
        *,
        request_id: str,
        message: str,
        selected_ip: str | None,
    ) -> Iterator[dict[str, Any]]:
        self._user()
        turn = self._turn(conversation, request_id=request_id, message=message)
        if not turn["user_persisted"]:
            self._product_call(
                lambda: self.client.append_message(conversation.conversation_id, "user", message)
            )
            turn["user_persisted"] = True
        if turn["stream_started"]:
            raise ProductCopilotStreamError("This Product turn was already submitted to Copilot.")
        turn["stream_started"] = True
        body: dict[str, Any] = {
            "conversation_id": conversation.conversation_id,
            "session_id": conversation.conversation_id,
            "request_id": request_id,
            "message": message,
        }
        ui_context = build_copilot_ui_context(selected_ip)
        if ui_context:
            body["ui_context"] = ui_context
        headers = {
            "Authorization": f"Bearer {self.client.token}",
            "Soorin_copilot_api_key": self.settings.copilot_api_key,
            "X-User-ID": self._user(),
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        session = self.http or requests.Session()
        started = time.perf_counter()
        logger.info("event=product_ui_stream_started request_id=%s", request_id)
        try:
            with session.post(
                f"{self.settings.api_base_url.rstrip('/')}/chat/stream",
                headers=headers,
                json=body,
                stream=True,
                timeout=self.settings.api_timeout_seconds,
            ) as response:
                if response.status_code == 401:
                    raise ProductCopilotStreamError("Copilot API authentication failed.")
                response.raise_for_status()
                response.encoding = "utf-8"
                for event in parse_sse_events(
                    response.iter_lines(chunk_size=1, decode_unicode=True)
                ):
                    if event.get("type") == "done":
                        turn["stream_done"] = True
                    yield event
        except ProductCopilotStreamError:
            raise
        except requests.RequestException as exc:
            raise ProductCopilotStreamError("The Copilot stream is unavailable.") from exc
        except ChatStreamProtocolError as exc:
            raise ProductCopilotStreamError("The Copilot stream returned invalid data.") from exc
        finally:
            logger.info(
                "event=product_ui_stream_finished request_id=%s stream_done=%s latency_ms=%s",
                request_id,
                turn["stream_done"],
                int((time.perf_counter() - started) * 1000),
            )

    def complete_display_turn(
        self,
        conversation: LocalConversation,
        *,
        request_id: str,
        user_content: str,
        assistant_content: str,
    ) -> list[dict[str, Any]]:
        del user_content
        turn = self.state.get(PRODUCT_TURN_KEY)
        if (
            not isinstance(turn, dict)
            or turn.get("request_id") != request_id
            or turn.get("room_id") != conversation.conversation_id
            or not turn.get("stream_done")
        ):
            raise ProductUserChatError("Product turn was not completed by Copilot.")
        turn["assistant_content"] = assistant_content
        if not turn["assistant_persisted"]:
            self._product_call(
                lambda: self.client.append_message(
                    conversation.conversation_id,
                    "assistant",
                    assistant_content,
                )
            )
            turn["assistant_persisted"] = True
        turn["completed"] = True
        return self.get_messages(conversation.conversation_id)

    def retry_pending_assistant(self, conversation_id: str) -> bool:
        turn = self.state.get(PRODUCT_TURN_KEY)
        if not isinstance(turn, dict) or turn.get("room_id") != conversation_id:
            return False
        if not turn.get("stream_done") or turn.get("assistant_persisted"):
            return False
        content = str(turn.get("assistant_content") or "").strip()
        if not content:
            return False
        self._product_call(
            lambda: self.client.append_message(conversation_id, "assistant", content)
        )
        turn["assistant_persisted"] = True
        turn["completed"] = True
        return True


def _client(settings: Settings) -> ProductUserChatClient:
    return ProductUserChatClient(
        settings,
        token=str(st.session_state.get(PRODUCT_TOKEN_KEY) or ""),
    )


def _login_page(settings: Settings) -> None:
    st.title("Soorin Copilot")
    st.caption("Product-backed SOC/NOC/NDR investigation workspace")
    with st.form("product_login_form", clear_on_submit=True):
        username = st.text_input("Username", key="product_login_username")
        password = st.text_input("Password", type="password", key="product_login_password")
        submitted = st.form_submit_button("Log in", type="primary", width="stretch")
    if submitted:
        try:
            session = ProductUserChatClient(settings).login(username, password)
            clear_product_ui_state(st.session_state)
            st.session_state[PRODUCT_TOKEN_KEY] = session.access_token
            st.session_state[PRODUCT_USER_ID_KEY] = session.user_id
            st.session_state[PRODUCT_USERNAME_KEY] = session.username
            st.session_state[PRODUCT_DISPLAY_NAME_KEY] = session.display_name
            st.session_state[PRODUCT_ROLE_KEY] = session.role
            logger.info("event=product_ui_login user_ref_set=true")
            st.rerun()
        except ProductUserChatError as exc:
            st.error(str(exc))


def _handle_ui_error(exc: ProductUserChatError) -> None:
    if isinstance(exc, ProductUserChatHTTPError) and exc.status_code == 401:
        clear_product_ui_state(st.session_state)
        st.warning("Your Product session expired. Please log in again.")
        st.rerun()
    st.error(str(exc))


def _sidebar(backend: ProductConversationBackend) -> None:
    with st.sidebar:
        if LOGO_PATH.exists():
            st.image(str(LOGO_PATH), width="stretch")
        st.subheader("Product chatrooms")
        identity = str(
            st.session_state.get(PRODUCT_DISPLAY_NAME_KEY)
            or st.session_state.get(PRODUCT_USERNAME_KEY)
            or "Product user"
        )
        st.caption(f"Signed in: `{identity}`")
        if st.button("New conversation", width="stretch"):
            try:
                conversation = backend.create_conversation("Asset investigation")
                st.session_state[PRODUCT_ROOM_KEY] = conversation.conversation_id
                st.session_state.pop(PRODUCT_TURN_KEY, None)
                logger.info("event=product_conversation_created conversation_ref_set=true")
                st.rerun()
            except ProductUserChatError as exc:
                _handle_ui_error(exc)
        try:
            conversations = backend.list_conversations()
        except ProductUserChatError as exc:
            _handle_ui_error(exc)
            conversations = []
        selected_id = str(st.session_state.get(PRODUCT_ROOM_KEY) or "")
        if not conversations:
            st.caption("No Product chatrooms yet. Create one to begin.")
        for conversation in conversations:
            if st.button(
                conversation.title or conversation.conversation_id,
                key=f"product_open_{conversation.conversation_id}",
                width="stretch",
            ):
                st.session_state[PRODUCT_ROOM_KEY] = conversation.conversation_id
                st.session_state.pop(PRODUCT_TURN_KEY, None)
                st.rerun()
        if selected_id:
            st.divider()
            if st.checkbox("Confirm delete selected conversation", key=PRODUCT_DELETE_CONFIRMATION_KEY):
                if st.button("Delete conversation", type="secondary", width="stretch"):
                    try:
                        backend.delete_conversation(selected_id)
                        st.session_state.pop(PRODUCT_ROOM_KEY, None)
                        st.session_state.pop(PRODUCT_TURN_KEY, None)
                        st.session_state.pop(PRODUCT_DELETE_CONFIRMATION_KEY, None)
                        logger.info("event=product_conversation_deleted conversation_ref_set=true")
                        st.rerun()
                    except ProductUserChatError as exc:
                        _handle_ui_error(exc)
        st.divider()
        if st.button("Log out", width="stretch"):
            backend.logout()
            logger.info("event=product_ui_logout")
            st.rerun()


def _chat(backend: ProductConversationBackend) -> None:
    room_id = str(st.session_state.get(PRODUCT_ROOM_KEY) or "")
    st.subheader("Investigation conversation")
    if not room_id:
        st.info("Create or select a Product chatroom to begin.")
        return
    try:
        conversation = backend.get_conversation(room_id)
    except ProductUserChatError as exc:
        _handle_ui_error(exc)
        return
    st.caption(f"Conversation: {conversation.title or conversation.conversation_id}")
    selected_ip = str(st.session_state.get("selected_copilot_ip") or "").strip() or None
    st.caption(f"Selected topology asset: {selected_ip or 'none'}")
    turn = st.session_state.get(PRODUCT_TURN_KEY)
    pending_here = isinstance(turn, dict) and turn.get("room_id") == room_id and not turn.get("completed")
    if pending_here and turn.get("stream_done") and not turn.get("assistant_persisted"):
        st.error("Copilot completed, but the assistant message was not saved to Product.")
        if st.button("Retry saving assistant response", width="stretch"):
            try:
                if backend.retry_pending_assistant(room_id):
                    st.success("Assistant response saved.")
                    st.rerun()
            except ProductUserChatError as exc:
                _handle_ui_error(exc)
        return
    if pending_here and turn.get("stream_started") and not turn.get("stream_done"):
        st.error("The user message was saved, but Copilot did not complete. No assistant message was saved.")
        if st.button("Acknowledge failed Copilot turn", width="stretch"):
            st.session_state.pop(PRODUCT_TURN_KEY, None)
            st.rerun()
        return
    if pending_here and not turn.get("user_persisted"):
        st.error("The Product user message was not confirmed saved, so Copilot was not called.")
        if st.button("Acknowledge failed Product write", width="stretch"):
            st.session_state.pop(PRODUCT_TURN_KEY, None)
            st.rerun()
        return
    controller = ConversationController(backend)
    completed = render_conversation_chat(
        controller,
        conversation,
        selected_ip=selected_ip,
        input_key=f"product_chat_input_{conversation.conversation_id}",
        streaming_key=PRODUCT_STREAMING_KEY,
        error_types=(ProductUserChatError, ProductCopilotStreamError, RuntimeError),
    )
    if not st.session_state.get(PRODUCT_TOKEN_KEY):
        st.rerun()
    if completed:
        logger.info("event=product_ui_stream_completed")
        st.rerun()


def run_product_workspace(settings: Settings) -> None:
    """Render Product login/chatrooms plus the shared investigation workspace."""
    if not st.session_state.get(PRODUCT_TOKEN_KEY) or not st.session_state.get(PRODUCT_USER_ID_KEY):
        clear_product_ui_state(st.session_state)
        _login_page(settings)
        return
    backend = ProductConversationBackend(settings, _client(settings), st.session_state)
    _sidebar(backend)
    st.title("Soorin Copilot")
    st.caption("Product-backed SOC/NOC/NDR investigation workspace")
    left, right = st.columns([0.42, 0.58], gap="large")
    with left:
        _chat(backend)
    with right:
        show_topology_page(embedded=True)
