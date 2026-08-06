"""Streamlit rendering for the disabled-by-default local development simulation."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

import streamlit as st

from src.config.settings import Settings
from src.web.copilot_help import render_copilot_help_button
from src.web.local_simulation import (
    LOCAL_CONVERSATION_KEY,
    LOCAL_DELETE_CONFIRMATION_KEY,
    LOCAL_MESSAGES_KEY,
    LOCAL_PENDING_REQUEST_KEY,
    LOCAL_STREAMING_KEY,
    LOCAL_USER_KEY,
    LocalConversation,
    LocalSimulationApiClient,
    LocalSimulationClientError,
    clear_local_ui_state,
)
from src.web.pages.topology import show_topology_page


logger = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parents[2]
LOGO_PATH = APP_DIR / "assets" / "branding" / "soorinsec-logo2.png"


def _client(settings: Settings) -> LocalSimulationApiClient:
    return LocalSimulationApiClient(
        api_base_url=settings.api_base_url,
        api_key=settings.copilot_api_key,
        timeout_seconds=settings.api_timeout_seconds,
    )


def _load_conversation(client: LocalSimulationApiClient, user_id: str, conversation_id: str) -> None:
    conversation = client.get_conversation(user_id, conversation_id)
    st.session_state[LOCAL_CONVERSATION_KEY] = conversation
    st.session_state[LOCAL_MESSAGES_KEY] = client.get_messages(user_id, conversation_id)
    st.session_state.pop(LOCAL_PENDING_REQUEST_KEY, None)
    st.session_state.pop(LOCAL_STREAMING_KEY, None)


def _login_page(client: LocalSimulationApiClient, settings: Settings) -> None:
    st.title("Soorin Copilot")
    st.caption("SOC/NOC/NDR investigation workspace")
    st.info("Local Development Simulation", icon="🧪")
    st.write("Select a local test identity. No password, Product account, or production authorization is used.")
    try:
        users = client.list_users()
    except LocalSimulationClientError as exc:
        st.error(str(exc))
        st.stop()
    user_ids = [str(item.get("user_id") or "") for item in users if item.get("user_id")]
    if user_ids:
        selected = st.selectbox("Local user", user_ids, key="local_simulation_login_user")
        if st.button("Sign in", type="primary", width="stretch"):
            try:
                client.get_user(selected)
                clear_local_ui_state(st.session_state)
                st.session_state[LOCAL_USER_KEY] = selected
                logger.info("event=local_ui_login user_ref_set=true")
                st.rerun()
            except LocalSimulationClientError as exc:
                st.error(str(exc))
    else:
        st.warning("No local users exist yet.")
    if settings.local_test_user_creation_enabled:
        with st.expander("Create local test user"):
            st.caption("Creates a local opaque identifier only. It does not create a Product user.")
            if st.button("Create test user", width="stretch"):
                try:
                    user = client.create_user()
                    st.session_state[LOCAL_USER_KEY] = str(user["user_id"])
                    logger.info("event=local_ui_login user_ref_set=true source=test_user_created")
                    st.rerun()
                except LocalSimulationClientError as exc:
                    st.error(str(exc))
    else:
        st.caption("Test-user creation is disabled by configuration.")
    st.warning("Local simulation data is stored only in the configured development SQLite database. Do not use it as production identity or authorization.")
    st.stop()


def _sidebar(client: LocalSimulationApiClient, user_id: str) -> None:
    with st.sidebar:
        if LOGO_PATH.exists():
            st.image(str(LOGO_PATH), width="stretch")
        st.subheader("Local chatrooms")
        st.caption(f"Signed in locally: `{user_id[:18]}`")
        if st.button("New conversation", width="stretch"):
            try:
                conversation = client.create_conversation(user_id, "Local investigation")
                st.session_state[LOCAL_CONVERSATION_KEY] = conversation
                st.session_state[LOCAL_MESSAGES_KEY] = []
                st.session_state.pop(LOCAL_DELETE_CONFIRMATION_KEY, None)
                logger.info("event=local_conversation_created conversation_ref_set=true")
                st.rerun()
            except LocalSimulationClientError as exc:
                st.error(str(exc))
        try:
            conversations = client.list_conversations(user_id)
        except LocalSimulationClientError as exc:
            st.error(str(exc))
            conversations = []
        selected = st.session_state.get(LOCAL_CONVERSATION_KEY)
        selected_id = selected.conversation_id if isinstance(selected, LocalConversation) else ""
        if not conversations:
            st.caption("No conversations yet. Create one to begin.")
        for conversation in conversations:
            label = conversation.title or "Untitled local conversation"
            if st.button(label, key=f"local_open_{conversation.conversation_id}", width="stretch"):
                try:
                    _load_conversation(client, user_id, conversation.conversation_id)
                    logger.info("event=local_conversation_opened conversation_ref_set=true")
                    st.rerun()
                except LocalSimulationClientError as exc:
                    st.error(str(exc))
        if selected_id:
            st.divider()
            if st.checkbox("Confirm delete selected conversation", key=LOCAL_DELETE_CONFIRMATION_KEY):
                if st.button("Delete conversation", type="secondary", width="stretch"):
                    try:
                        client.delete_conversation(user_id, selected_id)
                        clear_local_ui_state(st.session_state, keep_user=True)
                        logger.info("event=local_conversation_deleted conversation_ref_set=true")
                        st.rerun()
                    except LocalSimulationClientError as exc:
                        st.error(str(exc))
        st.divider()
        if st.button("Log out", width="stretch"):
            clear_local_ui_state(st.session_state)
            logger.info("event=local_ui_logout")
            st.rerun()


def _chat(client: LocalSimulationApiClient, user_id: str) -> None:
    conversation = st.session_state.get(LOCAL_CONVERSATION_KEY)
    st.subheader("Investigation conversation")
    if not isinstance(conversation, LocalConversation):
        st.info("Create or select a local conversation to begin.")
        return
    st.caption(f"Conversation: {conversation.title or conversation.conversation_id[:12]}")
    selected_ip = st.session_state.get("selected_copilot_ip")
    st.caption(f"Selected topology asset: {selected_ip or 'none'}")
    for item in st.session_state.get(LOCAL_MESSAGES_KEY, []):
        role = str(item.get("role") or "")
        if role in {"user", "assistant"}:
            with st.chat_message(role):
                st.markdown(str(item.get("content") or ""))
    render_copilot_help_button()
    streaming = bool(st.session_state.get(LOCAL_STREAMING_KEY))
    prompt = st.chat_input(
        "Ask a cybersecurity investigation question",
        key=f"local_chat_input_{conversation.conversation_id}",
        max_chars=4000,
        disabled=streaming,
        submit_mode="disable",
    )
    if not prompt:
        return
    request_id = uuid4().hex
    st.session_state[LOCAL_PENDING_REQUEST_KEY] = request_id
    st.session_state[LOCAL_STREAMING_KEY] = True
    with st.chat_message("user"):
        st.markdown(prompt)
    status = st.status("Connecting to Copilot", expanded=False)
    answer_parts: list[str] = []
    completed = False
    error = ""
    with st.chat_message("assistant"):
        placeholder = st.empty()
        try:
            status.update(label="Investigating", state="running", expanded=False)
            for event in client.stream_chat(
                user_id=user_id,
                conversation=conversation,
                request_id=request_id,
                message=prompt,
                selected_ip=selected_ip,
            ):
                if event.get("type") == "answer_delta" and isinstance(event.get("text"), str):
                    answer_parts.append(event["text"])
                    placeholder.markdown("".join(answer_parts) + "▌")
                elif event.get("type") == "done":
                    completed = True
                elif event.get("type") == "error":
                    error = str(event.get("message") or "The Copilot request failed.")
                    break
        except LocalSimulationClientError as exc:
            error = str(exc)
        finally:
            st.session_state[LOCAL_STREAMING_KEY] = False
            st.session_state.pop(LOCAL_PENDING_REQUEST_KEY, None)
    if completed:
        status.update(label="Response complete", state="complete", expanded=False)
        try:
            st.session_state[LOCAL_MESSAGES_KEY] = client.get_messages(user_id, conversation.conversation_id)
            logger.info("event=local_ui_stream_completed request_id=%s", request_id)
            st.rerun()
        except LocalSimulationClientError as exc:
            st.error(str(exc))
    else:
        status.update(label="Request failed", state="error", expanded=False)
        if answer_parts:
            placeholder.markdown("".join(answer_parts))
        st.error(error or "The Copilot stream ended before completion.")
        try:
            st.session_state[LOCAL_MESSAGES_KEY] = client.get_messages(user_id, conversation.conversation_id)
        except LocalSimulationClientError:
            pass
        logger.info("event=local_ui_stream_failed request_id=%s", request_id)


def run_local_simulation_workspace(settings: Settings) -> None:
    """Render the local-only login, chatroom, stream, and topology experience."""
    client = _client(settings)
    user_id = st.session_state.get(LOCAL_USER_KEY)
    if not user_id:
        _login_page(client, settings)
    try:
        client.get_user(str(user_id))
    except LocalSimulationClientError:
        clear_local_ui_state(st.session_state)
        _login_page(client, settings)
    _sidebar(client, str(user_id))
    st.title("Soorin Copilot")
    st.caption("Local Development Simulation for SOC/NOC/NDR investigations")
    left, right = st.columns([0.42, 0.58], gap="large")
    with left:
        _chat(client, str(user_id))
    with right:
        show_topology_page(embedded=True)
