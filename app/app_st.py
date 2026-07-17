"""Unified Streamlit investigation workspace for Soorin Cyber Copilot."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from uuid import uuid4

import requests
import streamlit as st

from src.config.settings import get_settings
from src.core.graph.loader import set_graph_path, load_graph
from src.web.chat_stream import ChatStreamProtocolError, parse_sse_events
from src.web.copilot_help import render_copilot_help_button
from src.web.pages.topology import build_copilot_ui_context, show_topology_page

logger = logging.getLogger(__name__)
settings = get_settings()
API_BASE_URL = settings.api_base_url
CHAT_URL = f"{API_BASE_URL}/chat"
CHAT_STREAM_URL = f"{API_BASE_URL}/chat/stream"
HEALTH_URL = f"{API_BASE_URL}/health"
LLM_HEALTH_URL = f"{API_BASE_URL}/llm/health"
REQUEST_TIMEOUT_SECONDS = settings.api_timeout_seconds
SELECTED_COPILOT_IP_KEY = "selected_copilot_ip"
LEGACY_SELECTED_IP_KEY = "copilot_graph_context_ip"
APP_DIR = Path(__file__).resolve().parent
SIDEBAR_LOGO_PATH = APP_DIR / "assets" / "branding" / "soorinsec-logo2.png"

# ============================================================
st.set_page_config(page_title="Soorin Cyber Copilot", layout="wide")

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
    if SELECTED_COPILOT_IP_KEY not in st.session_state:
        st.session_state[SELECTED_COPILOT_IP_KEY] = st.session_state.get(LEGACY_SELECTED_IP_KEY)
    st.session_state.pop(LEGACY_SELECTED_IP_KEY, None)

def clear_chat() -> None:
    st.session_state.messages = []
    st.session_state.session_id = uuid4().hex


def render_sidebar_branding() -> None:
    """Render the canonical company logo at the top of the sidebar."""
    if SIDEBAR_LOGO_PATH.exists():
        st.image(str(SIDEBAR_LOGO_PATH), width=180)


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


def get_active_llm_label() -> str:
    """Resolve the active chat model from backend metadata, then shared settings."""
    model = ""
    provider = ""
    try:
        response = requests.get(LLM_HEALTH_URL, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") == "ok":
            metadata = payload.get("data") or {}
            model = str(metadata.get("model") or "").strip()
            provider = str(metadata.get("provider") or "").strip()
    except (requests.RequestException, AttributeError, ValueError, TypeError):
        pass

    if not model:
        try:
            deployment = settings.deployment_for_purpose("chat")
            model = deployment.model.strip()
            provider = deployment.provider_type.strip()
        except (AttributeError, KeyError, TypeError, ValueError):
            return "unavailable"

    if not model:
        return "unavailable"
    return f"{model} ({provider.title()})" if provider else model


def ask_copilot(message: str) -> tuple[str | None, str | None]:
    started = time.perf_counter()
    payload = {"session_id": st.session_state.session_id, "message": message}
    ui_context = build_copilot_ui_context(st.session_state.get(SELECTED_COPILOT_IP_KEY))
    if ui_context:
        payload["ui_context"] = ui_context

    logger.info(
        "event=ui_chat_request session_id=%s message_chars=%s selected_ip_present=%s selected_ip=%s",
        st.session_state.session_id,
        len(message),
        bool((payload.get("ui_context") or {}).get("selected_ip")),
        (payload.get("ui_context") or {}).get("selected_ip") or "",
    )
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
    latency_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "event=ui_chat_response session_id=%s status=%s answer_chars=%s latency_ms=%s",
        st.session_state.session_id,
        payload.get("status"),
        len(answer),
        latency_ms,
    )
    return answer, None


class ChatStreamClientError(RuntimeError):
    """Safe user-facing streaming transport error."""


def stream_copilot(message: str):
    """Yield normalized SSE events from the final-model streaming endpoint."""
    started = time.perf_counter()
    payload = {"session_id": st.session_state.session_id, "message": message}
    ui_context = build_copilot_ui_context(st.session_state.get(SELECTED_COPILOT_IP_KEY))
    if ui_context:
        payload["ui_context"] = ui_context

    logger.info(
        "event=ui_chat_stream_request session_id=%s message_chars=%s selected_ip_present=%s selected_ip=%s",
        st.session_state.session_id,
        len(message),
        bool((payload.get("ui_context") or {}).get("selected_ip")),
        (payload.get("ui_context") or {}).get("selected_ip") or "",
    )
    try:
        with requests.post(
            CHAT_STREAM_URL,
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
            stream=True,
        ) as response:
            response.raise_for_status()
            for event in parse_sse_events(
                response.iter_lines(chunk_size=1, decode_unicode=True)
            ):
                yield event
    except requests.Timeout as exc:
        raise ChatStreamClientError("The Copilot request timed out.") from exc
    except requests.ConnectionError as exc:
        raise ChatStreamClientError(f"Backend API not reachable at {API_BASE_URL}.") from exc
    except requests.RequestException as exc:
        raise ChatStreamClientError("The backend API returned a streaming error.") from exc
    except ChatStreamProtocolError as exc:
        raise ChatStreamClientError(str(exc)) from exc
    finally:
        logger.info(
            "event=ui_chat_stream_end session_id=%s latency_ms=%s",
            st.session_state.session_id,
            int((time.perf_counter() - started) * 1000),
        )

init_session_state()

with st.sidebar:
    render_sidebar_branding()
    st.subheader("Workspace Status")
    st.caption("API URL")
    st.code(API_BASE_URL, language=None)

    if st.button("Check health", width="stretch"):
        healthy, health_message = get_backend_health()
        if healthy:
            st.success(health_message)
        else:
            st.error(health_message)

    if st.button("Clear chat", width="stretch"):
        clear_chat()
        st.rerun()

    st.divider()
    st.write(f"Backend: {'ready' if get_backend_health()[0] else 'offline'}")
    st.write(f"LLM: {get_active_llm_label()}")
    st.write(f"Graph: {'loaded' if graph_loaded else 'not found'} ({graph_node_count} nodes)")
    if st.session_state.get(SELECTED_COPILOT_IP_KEY):
        st.caption(f"Selected topology target: {st.session_state[SELECTED_COPILOT_IP_KEY]}")
    else:
        st.caption("Selected topology target: none")

st.title("Soorin Copilot")
st.caption("Asset Intelligence Investigation Workspace")

left_col, right_col = st.columns([0.42, 0.58], gap="large")

with left_col:
    st.title("Soorin Cyber Copilot")
    st.write(
        "A baseline cybersecurity copilot for general Q&A. "
        "Asset, RAG, and graph context will be added later."
    )
    selected_ip = st.session_state.get(SELECTED_COPILOT_IP_KEY)
    st.caption(f"Selected topology target: {selected_ip or 'none'}")
    st.caption("Graph context is used only on relevant graph or follow-up questions.")

    for item in st.session_state.messages:
        with st.chat_message(item["role"]):
            st.markdown(item["content"])

    submitted_turn = st.container()
    render_copilot_help_button()

    if prompt := st.chat_input("Ask a cybersecurity question"):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with submitted_turn:
            with st.chat_message("user"):
                st.markdown(prompt)
            thinking_status = st.status("Thinking...", expanded=True)
            with thinking_status:
                thinking_placeholder = st.empty()
                thinking_placeholder.caption("Waiting for the model response...")
            with st.chat_message("assistant"):
                answer_placeholder = st.empty()
                reasoning_parts: list[str] = []
                answer_parts: list[str] = []
                stream_complete = False
                stream_error = ""
                try:
                    for event in stream_copilot(prompt):
                        event_type = event.get("type")
                        text = event.get("text")
                        if event_type == "reasoning_delta" and isinstance(text, str):
                            reasoning_parts.append(text)
                            thinking_placeholder.markdown("".join(reasoning_parts))
                        elif event_type == "answer_delta" and isinstance(text, str):
                            answer_parts.append(text)
                            answer_placeholder.markdown("".join(answer_parts) + "▌")
                        elif event_type == "done":
                            stream_complete = True
                        elif event_type == "error":
                            stream_error = str(
                                event.get("message")
                                or "The Copilot could not complete the streamed response."
                            )
                            break
                except ChatStreamClientError as exc:
                    stream_error = str(exc)

                answer = "".join(answer_parts).strip()
                if stream_complete and answer:
                    answer_placeholder.markdown(answer)
                    thinking_status.update(
                        label="Thinking complete",
                        state="complete",
                        expanded=False,
                    )
                    st.session_state.messages.append({"role": "assistant", "content": answer})
                else:
                    stream_error = stream_error or "The Copilot stream ended without a complete answer."
                    if answer:
                        answer_placeholder.markdown(answer)
                    st.error(stream_error)
                    thinking_status.update(
                        label="Response interrupted",
                        state="error",
                        expanded=True,
                    )
                    if not answer:
                        st.session_state.messages.append(
                            {"role": "assistant", "content": stream_error}
                        )
        if stream_complete and answer or not answer:
            st.rerun()

with right_col:
    show_topology_page(embedded=True)
