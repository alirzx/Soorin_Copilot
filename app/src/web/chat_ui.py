"""Shared Streamlit chat rendering for all internal UI backends."""

from __future__ import annotations

from uuid import uuid4

import streamlit as st

from src.web.chat_backend import ConversationController
from src.web.copilot_help import render_copilot_help_button
from src.web.local_simulation import LocalConversation


def render_conversation_chat(
    controller: ConversationController,
    conversation: LocalConversation,
    *,
    selected_ip: str | None,
    input_key: str,
    streaming_key: str,
    error_types: tuple[type[Exception], ...] = (Exception,),
) -> bool:
    """Render messages then one input, and process one bounded SSE turn."""
    messages = controller.messages(conversation)
    for item in messages:
        role = str(item.get("role") or "")
        if role in {"user", "assistant"}:
            with st.chat_message(role):
                st.markdown(str(item.get("content") or ""))

    render_copilot_help_button()
    streaming = bool(st.session_state.get(streaming_key))
    prompt = st.chat_input(
        "Ask a cybersecurity investigation question",
        key=input_key,
        max_chars=4000,
        disabled=streaming,
        submit_mode="disable",
    )
    if not prompt:
        return False

    request_id = uuid4().hex
    st.session_state[streaming_key] = True
    with st.chat_message("user"):
        st.markdown(prompt)
    status = st.status("Connecting to Copilot", expanded=False)
    with status:
        reasoning_placeholder = st.empty()
    answer_parts: list[str] = []
    reasoning_parts: list[str] = []
    completed = False
    error = ""
    with st.chat_message("assistant"):
        placeholder = st.empty()
        try:
            status.update(label="Investigating", state="running", expanded=False)
            for event in controller.stream_turn(
                conversation,
                request_id=request_id,
                message=prompt,
                selected_ip=selected_ip,
            ):
                event_type = event.get("type")
                text = event.get("text")
                if event_type == "reasoning_delta" and isinstance(text, str):
                    reasoning_parts.append(text)
                    reasoning_placeholder.markdown("".join(reasoning_parts))
                elif event_type == "answer_delta" and isinstance(text, str):
                    answer_parts.append(text)
                    placeholder.markdown("".join(answer_parts) + "▌")
                elif event_type == "done":
                    completed = True
                elif event_type == "error":
                    error = str(event.get("message") or "The Copilot request failed.")
                    break
        except error_types as exc:
            error = str(exc)
        finally:
            st.session_state[streaming_key] = False

    answer = "".join(answer_parts).strip()
    if completed and answer:
        placeholder.markdown(answer)
        try:
            controller.complete_turn(
                conversation,
                request_id=request_id,
                user_content=prompt,
                assistant_content=answer,
            )
        except error_types as exc:
            status.update(label="Response could not be saved", state="error", expanded=False)
            st.error(str(exc))
            return False
        status.update(label="Response complete", state="complete", expanded=False)
        return True
    if answer:
        placeholder.markdown(answer)
    status.update(label="Request failed", state="error", expanded=False)
    st.error(error or "The Copilot stream ended before completion.")
    return False
