"""Streamlit UI for the baseline Soorin Cyber Copilot."""

from __future__ import annotations

from uuid import uuid4

import requests
import streamlit as st

from src.config.settings import get_settings

settings = get_settings()
API_BASE_URL = settings.api_base_url
CHAT_URL = f"{API_BASE_URL}/chat"
HEALTH_URL = f"{API_BASE_URL}/health"
REQUEST_TIMEOUT_SECONDS = settings.api_timeout_seconds


st.set_page_config(page_title="Soorin Cyber Copilot", layout="centered")


def init_session_state() -> None:
    if "session_id" not in st.session_state:
        st.session_state.session_id = uuid4().hex
    if "messages" not in st.session_state:
        st.session_state.messages = []


def clear_chat() -> None:
    st.session_state.messages = []
    st.session_state.session_id = uuid4().hex


def get_backend_health() -> tuple[bool, str]:
    try:
        response = requests.get(HEALTH_URL, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        return False, "Backend is not reachable."
    except ValueError:
        return False, "Backend returned an invalid health response."

    if payload.get("status") == "ok":
        return True, "Backend is healthy."
    return False, "Backend health check did not return ok."


def ask_copilot(message: str) -> tuple[str | None, str | None]:
    try:
        response = requests.post(
            CHAT_URL,
            json={
                "session_id": st.session_state.session_id,
                "message": message,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout:
        return None, "The Copilot request timed out. Please try again."
    except requests.ConnectionError:
        return None, f"The backend API is not reachable at {API_BASE_URL}."
    except requests.RequestException:
        return None, "The backend API returned an error."
    except ValueError:
        return None, "The backend API returned an invalid response."

    answer = ((payload.get("data") or {}).get("answer") or "").strip()
    if payload.get("status") != "ok" or not answer:
        return None, "The Copilot could not produce an answer."
    return answer, None


init_session_state()

with st.sidebar:
    st.subheader("Connection")
    st.caption("API URL")
    st.code(API_BASE_URL, language=None)
    st.caption("Request timeout")
    st.write(f"{REQUEST_TIMEOUT_SECONDS} seconds")
    st.caption("Configured UI port")
    st.write(str(settings.streamlit_server_port))

    if st.button("Check health", use_container_width=True):
        healthy, health_message = get_backend_health()
        if healthy:
            st.success(health_message)
        else:
            st.error(health_message)

    if st.button("Clear chat", use_container_width=True):
        clear_chat()
        st.rerun()

    st.subheader("Status")
    st.write("Backend: ready")
    st.write("GLM-5.2: via Arvan")
    st.write("RAG: planned")
    st.write("Graph context: planned")


st.title("Soorin Cyber Copilot")
st.write(
    "A baseline cybersecurity copilot for general Q&A. "
    "Asset, RAG, and graph context will be added later."
)

for item in st.session_state.messages:
    with st.chat_message(item["role"]):
        st.markdown(item["content"])

if prompt := st.chat_input("Ask a cybersecurity question"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            answer, error = ask_copilot(prompt)
        if error:
            st.error(error)
            st.session_state.messages.append({"role": "assistant", "content": error})
        else:
            st.markdown(answer)
            st.session_state.messages.append({"role": "assistant", "content": answer})
