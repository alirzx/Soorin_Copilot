"""Streamlit UI for the Soorin Cyber Copilot — with Topology page."""

from __future__ import annotations

from uuid import uuid4

import requests
import streamlit as st

from src.config.settings import get_settings
from src.core.graph.loader import set_graph_path, load_graph

settings = get_settings()
API_BASE_URL = settings.api_base_url
CHAT_URL = f"{API_BASE_URL}/chat"
HEALTH_URL = f"{API_BASE_URL}/health"
REQUEST_TIMEOUT_SECONDS = settings.api_timeout_seconds

# ============================================================
# PAGE CONFIG
# ============================================================
st.set_page_config(page_title="Soorin Cyber Copilot", layout="centered")

# ============================================================
# INIT GRAPH (Load once at startup)
# ============================================================
@st.cache_resource
def init_graph():
    """Load and cache the topology graph."""
    set_graph_path(settings.graph_pickle_path)
    return load_graph()

# Load graph on first run
try:
    _graph = init_graph()
    graph_loaded = True
    graph_node_count = _graph.number_of_nodes()
except FileNotFoundError:
    graph_loaded = False
    graph_node_count = 0

# ============================================================
# SESSION STATE
# ============================================================
def init_session_state() -> None:
    if "session_id" not in st.session_state:
        st.session_state.session_id = uuid4().hex
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "copilot_graph_context_ip" not in st.session_state:
        st.session_state.copilot_graph_context_ip = None

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
    payload = {"session_id": st.session_state.session_id, "message": message}
    if st.session_state.get("copilot_graph_context_ip"):
        payload["ui_context"] = {"selected_ip": st.session_state.copilot_graph_context_ip}

    try:
        response = requests.post(
            CHAT_URL,
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout:
        return None, "The Copilot request timed out."
    except requests.ConnectionError:
        return None, f"Backend API not reachable at {API_BASE_URL}."
    except requests.RequestException:
        return None, "The backend API returned an error."
    except ValueError:
        return None, "The backend API returned an invalid response."
    answer = ((payload.get("data") or {}).get("answer") or "").strip()
    if payload.get("status") != "ok" or not answer:
        return None, "The Copilot could not produce an answer."
    return answer, None

init_session_state()

# ============================================================
# NAVIGATION
# ============================================================
pages = {
    "Copilot Chat": "chat",
    "Network Topology": "topology",
}
page = st.sidebar.radio("Navigation", list(pages.keys()))

# ============================================================
# SIDEBAR
# ============================================================
with st.sidebar:
    st.divider()
    st.subheader("Connection")
    st.caption("API URL")
    st.code(API_BASE_URL, language=None)
    st.caption("Request timeout")
    st.write(f"{REQUEST_TIMEOUT_SECONDS} seconds")

    if st.button("Check health", use_container_width=True):
        healthy, health_message = get_backend_health()
        if healthy:
            st.success(health_message)
        else:
            st.error(health_message)

    if page == "Copilot Chat":
        if st.button("Clear chat", use_container_width=True):
            clear_chat()
            st.rerun()

    st.divider()
    st.subheader("Status")
    st.write(f"Backend: {'ready' if get_backend_health()[0] else 'offline'}")
    st.write("LLM: GLM-5.2 (Arvan)")
    st.write(f"Graph: {'loaded' if graph_loaded else 'not found'} ({graph_node_count} nodes)")
    if st.session_state.get("copilot_graph_context_ip"):
        st.caption(f"Copilot graph context: {st.session_state.copilot_graph_context_ip}")
    st.write("RAG: planned")

# ============================================================
# COPILOT CHAT PAGE
# ============================================================
if page == "Copilot Chat":
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

# ============================================================
# TOPOLOGY PAGE
# ============================================================
elif page == "Network Topology":
    from src.web.pages.topology import show_topology_page
    show_topology_page()
