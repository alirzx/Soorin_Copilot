"""Streamlit rendering for the disabled-by-default local development simulation."""

from __future__ import annotations

import logging
from pathlib import Path
import streamlit as st

from src.config.settings import Settings
from src.web.chat_backend import ConversationController, LocalSimulationBackend
from src.web.chat_ui import render_conversation_chat
from src.web.local_simulation import (
    LOCAL_CONVERSATION_KEY,
    LOCAL_DELETE_CONFIRMATION_KEY,
    LOCAL_MESSAGES_KEY,
    LOCAL_PENDING_REQUEST_KEY,
    LOCAL_STREAMING_KEY,
    LOCAL_USER_KEY,
    LOCAL_USERNAME_KEY,
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
    st.write("Sign in with a local development account. This does not use or change Product authentication.")
    login_tab, signup_tab = st.tabs(["Login", "Sign Up"])
    with login_tab:
        with st.form("local_simulation_login_form", clear_on_submit=False):
            username = st.text_input("Username", key="local_login_username")
            password = st.text_input("Password", type="password", key="local_login_password")
            submitted = st.form_submit_button("Login", type="primary", width="stretch")
        if submitted:
            try:
                user = client.login(username, password)
                clear_local_ui_state(st.session_state)
                st.session_state[LOCAL_USER_KEY] = str(user["user_id"])
                st.session_state[LOCAL_USERNAME_KEY] = str(user["username"])
                logger.info("event=local_ui_login user_ref_set=true")
                st.rerun()
            except LocalSimulationClientError as exc:
                st.error(str(exc))
    with signup_tab:
        if settings.local_test_user_creation_enabled:
            with st.form("local_simulation_signup_form", clear_on_submit=False):
                new_username = st.text_input("Username", key="local_signup_username")
                new_password = st.text_input("Password", type="password", key="local_signup_password")
                confirm_password = st.text_input(
                    "Confirm password",
                    type="password",
                    key="local_signup_confirm_password",
                )
                create_submitted = st.form_submit_button("Create account", width="stretch")
            if create_submitted:
                try:
                    user = client.create_user(new_username, new_password, confirm_password)
                    clear_local_ui_state(st.session_state)
                    st.session_state[LOCAL_USER_KEY] = str(user["user_id"])
                    st.session_state[LOCAL_USERNAME_KEY] = str(user["username"])
                    logger.info("event=local_ui_login user_ref_set=true source=signup")
                    st.rerun()
                except LocalSimulationClientError as exc:
                    st.error(str(exc))
        else:
            st.caption("Local account creation is disabled by configuration.")
    st.warning("Local simulation data is stored only in the configured development SQLite database. Do not use it as production identity or authorization.")
    st.stop()


def _sidebar(client: LocalSimulationApiClient, user_id: str, username: str) -> None:
    with st.sidebar:
        if LOGO_PATH.exists():
            st.image(str(LOGO_PATH), width="stretch")
        st.subheader("Local chatrooms")
        st.caption(f"Signed in locally: `{username}`")
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
    backend = LocalSimulationBackend(client, st.session_state)
    controller = ConversationController(backend)
    if render_conversation_chat(
        controller,
        conversation,
        selected_ip=selected_ip,
        input_key=f"local_chat_input_{conversation.conversation_id}",
        streaming_key=LOCAL_STREAMING_KEY,
        error_types=(LocalSimulationClientError, RuntimeError),
    ):
        st.session_state[LOCAL_MESSAGES_KEY] = backend.get_messages(conversation.conversation_id)
        logger.info("event=local_ui_stream_completed")
        st.rerun()


def run_local_simulation_workspace(settings: Settings) -> None:
    """Render the local-only login, chatroom, stream, and topology experience."""
    client = _client(settings)
    user_id = st.session_state.get(LOCAL_USER_KEY)
    if not user_id:
        _login_page(client, settings)
    try:
        user = client.get_user(str(user_id))
        st.session_state[LOCAL_USERNAME_KEY] = str(user.get("username") or "")
    except LocalSimulationClientError:
        clear_local_ui_state(st.session_state)
        _login_page(client, settings)
    _sidebar(
        client,
        str(user_id),
        str(st.session_state.get(LOCAL_USERNAME_KEY) or "local user"),
    )
    st.title("Soorin Copilot")
    st.caption("Local Development Simulation for SOC/NOC/NDR investigations")
    left, right = st.columns([0.42, 0.58], gap="large")
    with left:
        _chat(client, str(user_id))
    with right:
        show_topology_page(embedded=True)
