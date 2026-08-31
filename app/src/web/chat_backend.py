"""Small UI-side backend contract shared by legacy and local simulation modes."""

from __future__ import annotations

from collections.abc import Callable, Iterator, MutableMapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from src.web.local_simulation import (
    LocalConversation,
    LocalSimulationApiClient,
    clear_local_ui_state,
)


class ChatBackend(Protocol):
    def current_user(self) -> str | None: ...
    def login(self, user_id: str) -> None: ...
    def logout(self) -> None: ...
    def list_conversations(self) -> list[LocalConversation]: ...
    def create_conversation(self, title: str = "") -> LocalConversation: ...
    def get_conversation(self, conversation_id: str) -> LocalConversation: ...
    def get_messages(self, conversation_id: str) -> list[dict[str, Any]]: ...
    def delete_conversation(self, conversation_id: str) -> None: ...
    def stream_chat(
        self,
        conversation: LocalConversation,
        *,
        request_id: str,
        message: str,
        selected_ip: str | None,
    ) -> Iterator[dict[str, Any]]: ...
    def complete_display_turn(
        self,
        conversation: LocalConversation,
        *,
        request_id: str,
        user_content: str,
        assistant_content: str,
    ) -> list[dict[str, Any]]: ...


@dataclass
class LocalSimulationBackend:
    client: LocalSimulationApiClient
    state: MutableMapping[str, Any]

    def current_user(self) -> str | None:
        return self.state.get("local_simulation_user_id")

    def login(self, user_id: str) -> None:
        self.client.get_user(user_id)
        clear_local_ui_state(self.state)
        self.state["local_simulation_user_id"] = user_id

    def logout(self) -> None:
        clear_local_ui_state(self.state)

    def _user(self) -> str:
        user_id = self.current_user()
        if not user_id:
            raise RuntimeError("A local simulation user is required.")
        return user_id

    def list_conversations(self) -> list[LocalConversation]:
        return self.client.list_conversations(self._user())

    def create_conversation(self, title: str = "") -> LocalConversation:
        return self.client.create_conversation(self._user(), title)

    def get_conversation(self, conversation_id: str) -> LocalConversation:
        return self.client.get_conversation(self._user(), conversation_id)

    def get_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        return self.client.get_messages(self._user(), conversation_id)

    def delete_conversation(self, conversation_id: str) -> None:
        self.client.delete_conversation(self._user(), conversation_id)

    def stream_chat(self, conversation: LocalConversation, **kwargs: Any) -> Iterator[dict[str, Any]]:
        return self.client.stream_chat(user_id=self._user(), conversation=conversation, **kwargs)

    def complete_display_turn(self, conversation: LocalConversation, **_: Any) -> list[dict[str, Any]]:
        return self.get_messages(conversation.conversation_id)


@dataclass
class LegacyDirectBackend:
    """Compatibility adapter for the existing single-session developer UI."""

    state: MutableMapping[str, Any]
    stream: Callable[[str], Iterator[dict[str, Any]]]

    def current_user(self) -> str | None:
        return "legacy-developer"

    def login(self, user_id: str) -> None:
        del user_id

    def logout(self) -> None:
        self.state["messages"] = []
        self.state["session_id"] = uuid4().hex

    def _conversation(self) -> LocalConversation:
        session_id = str(self.state.get("session_id") or uuid4().hex)
        self.state["session_id"] = session_id
        return LocalConversation(
            conversation_id=session_id,
            user_id="legacy-developer",
            session_id=session_id,
            title="Current session",
            created_at="",
            updated_at="",
        )

    def list_conversations(self) -> list[LocalConversation]:
        return [self._conversation()]

    def create_conversation(self, title: str = "") -> LocalConversation:
        del title
        self.logout()
        return self._conversation()

    def get_conversation(self, conversation_id: str) -> LocalConversation:
        conversation = self._conversation()
        if conversation.conversation_id != conversation_id:
            raise KeyError("Unknown legacy conversation.")
        return conversation

    def get_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        self.get_conversation(conversation_id)
        return list(self.state.get("messages") or [])

    def delete_conversation(self, conversation_id: str) -> None:
        self.get_conversation(conversation_id)
        self.logout()

    def stream_chat(self, conversation: LocalConversation, **kwargs: Any) -> Iterator[dict[str, Any]]:
        self.get_conversation(conversation.conversation_id)
        return self.stream(str(kwargs["message"]))

    def complete_display_turn(
        self,
        conversation: LocalConversation,
        *,
        request_id: str,
        user_content: str,
        assistant_content: str,
    ) -> list[dict[str, Any]]:
        del request_id
        self.get_conversation(conversation.conversation_id)
        messages = list(self.state.get("messages") or [])
        messages.extend(
            (
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": assistant_content},
            )
        )
        self.state["messages"] = messages
        return messages


@dataclass(frozen=True)
class ConversationController:
    backend: ChatBackend

    def messages(self, conversation: LocalConversation) -> list[dict[str, Any]]:
        return self.backend.get_messages(conversation.conversation_id)

    def stream_turn(
        self,
        conversation: LocalConversation,
        *,
        request_id: str,
        message: str,
        selected_ip: str | None,
    ) -> Iterator[dict[str, Any]]:
        return self.backend.stream_chat(
            conversation,
            request_id=request_id,
            message=message,
            selected_ip=selected_ip,
        )

    def complete_turn(
        self,
        conversation: LocalConversation,
        *,
        request_id: str,
        user_content: str,
        assistant_content: str,
    ) -> list[dict[str, Any]]:
        return self.backend.complete_display_turn(
            conversation,
            request_id=request_id,
            user_content=user_content,
            assistant_content=assistant_content,
        )
