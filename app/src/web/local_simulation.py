"""HTTP client and rerun-state helpers for the opt-in local Streamlit simulation."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import requests

from src.web.chat_stream import ChatStreamProtocolError, parse_sse_events


logger = logging.getLogger(__name__)
LOCAL_USER_KEY = "local_simulation_user_id"
LOCAL_USERNAME_KEY = "local_simulation_username"
LOCAL_CONVERSATION_KEY = "local_simulation_conversation"
LOCAL_MESSAGES_KEY = "local_simulation_messages"
LOCAL_PENDING_REQUEST_KEY = "local_simulation_pending_request_id"
LOCAL_STREAMING_KEY = "local_simulation_streaming"
LOCAL_DELETE_CONFIRMATION_KEY = "local_simulation_delete_confirmation"
LOCAL_TRANSIENT_KEY = "local_simulation_transient_response"
LOCAL_STATE_KEYS = (
    LOCAL_USER_KEY,
    LOCAL_USERNAME_KEY,
    LOCAL_CONVERSATION_KEY,
    LOCAL_MESSAGES_KEY,
    LOCAL_PENDING_REQUEST_KEY,
    LOCAL_STREAMING_KEY,
    LOCAL_DELETE_CONFIRMATION_KEY,
    LOCAL_TRANSIENT_KEY,
    "selected_copilot_ip",
    "topology_graph_last_selection_event_id",
)


class LocalSimulationClientError(RuntimeError):
    """Safe user-facing local simulation API error."""


def copilot_auth_headers(api_key: str) -> dict[str, str]:
    """Preserve Bearer support while preferring the custom Product-compatible key."""
    key = str(api_key or "").strip()
    if not key:
        return {}
    return {
        "Authorization": f"Bearer {key}",
        "Soorin_copilot_api_key": key,
    }


def clear_local_ui_state(state: Any, *, keep_user: bool = False) -> None:
    """Clear rerun-only state when logging out, switching users, or deleting a chat."""
    for key in LOCAL_STATE_KEYS:
        if keep_user and key in {LOCAL_USER_KEY, LOCAL_USERNAME_KEY}:
            continue
        state.pop(key, None)


@dataclass(frozen=True)
class LocalConversation:
    conversation_id: str
    user_id: str
    session_id: str
    title: str
    created_at: str
    updated_at: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "LocalConversation":
        try:
            return cls(
                conversation_id=str(payload["conversation_id"]),
                user_id=str(payload["user_id"]),
                session_id=str(payload["session_id"]),
                title=str(payload.get("title") or ""),
                created_at=str(payload["created_at"]),
                updated_at=str(payload["updated_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalSimulationClientError("The local simulation returned invalid conversation data.") from exc


class LocalSimulationApiClient:
    """Small client for local-simulation metadata and the existing SSE endpoint."""

    def __init__(self, *, api_base_url: str, api_key: str, timeout_seconds: int) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.headers = copilot_auth_headers(api_key)

    def _request(
        self,
        method: str,
        path: str,
        *,
        user_id: str | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = dict(self.headers)
        if user_id:
            headers["X-User-ID"] = user_id
        try:
            response = requests.request(
                method,
                f"{self.api_base_url}{path}",
                headers=headers,
                json=json_body,
                params=params,
                timeout=self.timeout_seconds,
            )
            if response.status_code >= 400:
                try:
                    detail = str((response.json() or {}).get("detail") or "")
                except ValueError:
                    detail = ""
                raise LocalSimulationClientError(
                    detail or "The local simulation request was rejected."
                )
            payload = response.json()
        except LocalSimulationClientError:
            raise
        except requests.RequestException as exc:
            raise LocalSimulationClientError("The local simulation API is unavailable.") from exc
        except ValueError as exc:
            raise LocalSimulationClientError("The local simulation API returned invalid data.") from exc
        if not isinstance(payload, dict):
            raise LocalSimulationClientError("The local simulation API returned invalid data.")
        if payload.get("status") in {"not_enabled", "creation_disabled"}:
            raise LocalSimulationClientError(str(payload.get("detail") or "Local simulation is unavailable."))
        return payload

    def list_users(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "/local-simulation/users").get("users") or [])

    def create_user(self, username: str, password: str, confirm_password: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/local-simulation/users",
            json_body={
                "username": username,
                "password": password,
                "confirm_password": confirm_password,
            },
        )

    def login(self, username: str, password: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/local-simulation/login",
            json_body={"username": username, "password": password},
        )

    def get_user(self, user_id: str) -> dict[str, Any]:
        return self._request("GET", f"/local-simulation/users/{user_id}")

    def list_conversations(self, user_id: str) -> list[LocalConversation]:
        payload = self._request("GET", "/local-simulation/conversations", user_id=user_id)
        return [LocalConversation.from_payload(item) for item in payload.get("conversations") or []]

    def create_conversation(self, user_id: str, title: str = "") -> LocalConversation:
        return LocalConversation.from_payload(
            self._request(
                "POST",
                "/local-simulation/conversations",
                user_id=user_id,
                json_body={"title": title},
            )
        )

    def get_conversation(self, user_id: str, conversation_id: str) -> LocalConversation:
        return LocalConversation.from_payload(
            self._request(
                "GET",
                f"/local-simulation/conversations/{conversation_id}",
                user_id=user_id,
            )
        )

    def get_messages(self, user_id: str, conversation_id: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"/local-simulation/conversations/{conversation_id}/messages",
            user_id=user_id,
            params={"limit": 200},
        )
        return list(payload.get("messages") or [])

    def delete_conversation(self, user_id: str, conversation_id: str) -> None:
        self._request(
            "DELETE",
            f"/local-simulation/conversations/{conversation_id}",
            user_id=user_id,
        )

    def stream_chat(
        self,
        *,
        user_id: str,
        conversation: LocalConversation,
        request_id: str,
        message: str,
        selected_ip: str | None,
    ) -> Iterator[dict[str, Any]]:
        payload: dict[str, Any] = {
            "session_id": conversation.session_id,
            "conversation_id": conversation.conversation_id,
            "request_id": request_id,
            "message": message,
            "ui_context": {"selected_ip": selected_ip},
        }
        headers = dict(self.headers)
        headers["X-User-ID"] = user_id
        started = time.perf_counter()
        logger.info("event=local_ui_stream_started request_id=%s", request_id)
        try:
            with requests.post(
                f"{self.api_base_url}/chat/stream",
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
                stream=True,
            ) as response:
                response.raise_for_status()
                response.encoding = "utf-8"
                yield from parse_sse_events(response.iter_lines(chunk_size=1, decode_unicode=True))
        except requests.RequestException as exc:
            raise LocalSimulationClientError("The Copilot stream is unavailable.") from exc
        except ChatStreamProtocolError as exc:
            raise LocalSimulationClientError("The Copilot stream returned invalid data.") from exc
        finally:
            logger.info(
                "event=local_ui_stream_finished request_id=%s latency_ms=%s",
                request_id,
                int((time.perf_counter() - started) * 1000),
            )
