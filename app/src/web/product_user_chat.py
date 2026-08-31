"""Per-Streamlit-session Product authentication and canonical chatroom transport."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import requests

from src.config.settings import Settings


class ProductUserChatError(RuntimeError):
    """Bounded user-facing Product authentication/chat error."""


class ProductUserChatHTTPError(ProductUserChatError):
    def __init__(self, operation: str, status_code: int) -> None:
        labels = {
            401: "authentication expired",
            403: "access was forbidden",
            404: "resource was not found",
            422: "request validation failed",
        }
        label = labels.get(status_code, "backend request failed")
        super().__init__(f"Product {operation} {label}.")
        self.operation = operation
        self.status_code = status_code


@dataclass(frozen=True)
class ProductLoginSession:
    access_token: str
    user_id: str
    username: str = ""
    display_name: str = ""
    role: str = ""


@dataclass(frozen=True)
class ProductChatMessage:
    role: str
    content: str
    created_at: str = ""

    @classmethod
    def from_payload(cls, value: Any) -> "ProductChatMessage":
        if not isinstance(value, dict):
            raise ProductUserChatError("Product returned invalid chat message data.")
        role = str(value.get("role") or "").strip().lower()
        content = str(value.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            raise ProductUserChatError("Product returned invalid chat message data.")
        return cls(
            role=role,
            content=content,
            created_at=str(value.get("createdAt") or value.get("created_at") or ""),
        )


@dataclass(frozen=True)
class ProductChatRoom:
    room_id: str
    title: str = ""
    asset_ip: str = ""
    messages: tuple[ProductChatMessage, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_payload(cls, value: Any) -> "ProductChatRoom":
        if not isinstance(value, dict):
            raise ProductUserChatError("Product returned invalid chatroom data.")
        room_id = str(value.get("roomId") or value.get("id") or "").strip()
        if not room_id:
            raise ProductUserChatError("Product chatroom response omitted the room ID.")
        raw_messages = value.get("messages") or ()
        if not isinstance(raw_messages, (list, tuple)):
            raise ProductUserChatError("Product returned invalid chatroom messages.")
        return cls(
            room_id=room_id,
            title=str(value.get("title") or ""),
            asset_ip=str(value.get("assetIp") or value.get("asset_ip") or ""),
            messages=tuple(ProductChatMessage.from_payload(item) for item in raw_messages),
            created_at=str(value.get("createdAt") or value.get("created_at") or ""),
            updated_at=str(value.get("updatedAt") or value.get("updated_at") or ""),
        )


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and isinstance(value.get("data"), (dict, list)):
        return value["data"]
    return value


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        segment = token.split(".")[1]
        padded = segment + "=" * (-len(segment) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        claims = json.loads(decoded)
    except (IndexError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductUserChatError("Product login did not provide a usable user identity.") from exc
    if not isinstance(claims, dict):
        raise ProductUserChatError("Product login did not provide a usable user identity.")
    return claims


def product_user_id(payload: dict[str, Any], token: str) -> str:
    candidates: list[Any] = [payload.get("userId"), payload.get("user_id")]
    nested_user = payload.get("user")
    if isinstance(nested_user, dict):
        candidates.extend((nested_user.get("userId"), nested_user.get("user_id"), nested_user.get("id")))
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        if normalized:
            return normalized
    subject = str(_jwt_claims(token).get("sub") or "").strip()
    if subject:
        return subject
    raise ProductUserChatError("Product login did not provide a usable user identity.")


class ProductUserChatClient:
    """Uses only the interactive Product user's in-memory bearer token."""

    def __init__(
        self,
        settings: Settings,
        token: str = "",
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.token = str(token or "").removeprefix("Bearer ").strip()
        self.session = session or requests.Session()

    def _url(self, path: str) -> str:
        if not self.settings.product_api_base_url:
            raise ProductUserChatError("Product API base URL is not configured.")
        return f"{self.settings.product_api_base_url.rstrip('/')}/{path.lstrip('/')}"

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        if not self.token:
            raise ProductUserChatError("Product user authentication is required.")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "x-hwid": self.settings.product_hwid,
            "Accept": "application/json",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _payload(response: requests.Response, *, operation: str) -> Any:
        if response.status_code >= 400:
            raise ProductUserChatHTTPError(operation, response.status_code)
        if response.status_code == 204:
            return None
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProductUserChatError(f"Product {operation} returned invalid JSON.") from exc
        if not isinstance(payload, (dict, list)):
            raise ProductUserChatError(f"Product {operation} returned invalid data.")
        return _unwrap(payload)

    def login(self, username: str, password: str) -> ProductLoginSession:
        username = str(username or "").strip()
        if not username or not password:
            raise ProductUserChatError("Product username and password are required.")
        try:
            response = self.session.post(
                self._url(self.settings.product_login_path),
                headers={
                    "x-hwid": self.settings.product_hwid,
                    "x-captcha-bypass": self.settings.product_captcha_bypass,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={"username": username, "password": password},
                timeout=(
                    self.settings.product_connect_timeout_seconds,
                    self.settings.product_read_timeout_seconds,
                ),
            )
        except requests.RequestException as exc:
            raise ProductUserChatError("Product login is unavailable.") from exc
        payload = self._payload(response, operation="login")
        if not isinstance(payload, dict):
            raise ProductUserChatError("Product login returned invalid data.")
        token = str(payload.get("accessToken") or "").removeprefix("Bearer ").strip()
        if not token:
            raise ProductUserChatError("Product login did not return an access token.")
        user_id = product_user_id(payload, token)
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        return ProductLoginSession(
            access_token=token,
            user_id=user_id,
            username=str(user.get("username") or payload.get("username") or ""),
            display_name=str(user.get("fullName") or payload.get("fullName") or ""),
            role=str(user.get("role") or payload.get("role") or ""),
        )

    def _request(
        self,
        method: str,
        suffix: str = "",
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, int] | None = None,
    ) -> Any:
        path = self.settings.product_chat_rooms_path.rstrip("/")
        if suffix:
            path = f"{path}/{suffix.lstrip('/')}"
        try:
            response = self.session.request(
                method,
                self._url(path),
                headers=self._headers(json_body=body is not None),
                json=body,
                params=params,
                timeout=(
                    self.settings.product_connect_timeout_seconds,
                    self.settings.product_read_timeout_seconds,
                ),
            )
        except requests.RequestException as exc:
            raise ProductUserChatError("Product chat service is unavailable.") from exc
        return self._payload(response, operation="chat")

    def list_rooms(self, *, limit: int = 50, offset: int = 0) -> list[ProductChatRoom]:
        payload = self._request("GET", params={"limit": int(limit), "offset": int(offset)})
        if isinstance(payload, dict):
            payload = payload.get("rooms", payload.get("items"))
        if not isinstance(payload, list):
            raise ProductUserChatError("Product chatroom list returned invalid data.")
        return [ProductChatRoom.from_payload(item) for item in payload]

    def create_room(self, title: str, asset_ip: str = "") -> ProductChatRoom:
        payload = self._request(
            "POST",
            body={"title": str(title or "").strip(), "assetIp": str(asset_ip or "").strip()},
        )
        return ProductChatRoom.from_payload(payload)

    def get_room(self, room_id: str) -> ProductChatRoom:
        room_id = str(room_id or "").strip()
        if not room_id:
            raise ProductUserChatError("Product chatroom ID is required.")
        return ProductChatRoom.from_payload(self._request("GET", room_id))

    def append_message(self, room_id: str, role: str, content: str) -> Any:
        room_id = str(room_id or "").strip()
        role = str(role or "").strip().lower()
        content = str(content or "").strip()
        if not room_id or role not in {"user", "assistant"} or not content:
            raise ProductUserChatError("Product chat message is invalid.")
        return self._request(
            "POST",
            f"{room_id}/messages",
            body={"role": role, "content": content},
        )

    def delete_room(self, room_id: str) -> None:
        room_id = str(room_id or "").strip()
        if not room_id:
            raise ProductUserChatError("Product chatroom ID is required.")
        self._request("DELETE", room_id)
