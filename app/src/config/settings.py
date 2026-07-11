"""Simple environment-based settings for the Copilot backend."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = APP_DIR / ".env"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


@dataclass(frozen=True)
class Settings:
    api_host: str
    api_port: int
    api_reload: bool
    log_level: str
    api_base_url: str
    api_timeout_seconds: int
    streamlit_server_port: int
    llm_enabled: bool
    llm_provider: str
    arvan_base_url: str
    arvan_model: str
    arvan_api_key: str
    arvan_auth_scheme: str
    arvan_chat_path: str
    arvan_timeout_seconds: int
    arvan_connect_timeout_seconds: int
    arvan_max_tokens: int
    arvan_temperature: float
    arvan_top_p: float
    llm_expose_reasoning: bool
    llm_log_raw_response: bool
    chat_store_history: bool
    chat_max_history_messages: int
    conversation_max_messages: int
    system_prompt_path: str
    product_api_base_url: str
    product_topology_path: str
    product_api_token: str
    product_hwid: str
    product_connect_timeout_seconds: int
    product_read_timeout_seconds: int
    product_max_retries: int
    product_retry_backoff_seconds: float
    graph_raw_path: str
    graph_pickle_path: str
    graph_stats_path: str
    graph_graphml_path: str
    graph_gexf_path: str
    graph_max_ui_nodes: int
    graph_default_min_degree: int
    graph_api_max_neighbors: int
    intent_router_enabled: bool
    intent_router_timeout_seconds: int
    intent_router_min_confidence: float
    copilot_human_trace_enabled: bool


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_env_file(ENV_PATH)
    return Settings(
        api_host=os.getenv("API_HOST", "0.0.0.0"),
        api_port=_int("API_PORT", 6998),
        api_reload=_bool("API_RELOAD", True),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        api_base_url=os.getenv("SOORIN_API_BASE_URL", "http://127.0.0.1:6998").strip().rstrip("/"),
        api_timeout_seconds=_int("SOORIN_API_TIMEOUT_SECONDS", 120),
        streamlit_server_port=_int("STREAMLIT_SERVER_PORT", 8503),
        llm_enabled=_bool("SOORIN_LLM_ENABLED", True),
        llm_provider=os.getenv("SOORIN_LLM_PROVIDER", "arvan").strip().lower(),
        arvan_base_url=os.getenv("SOORIN_ARVAN_BASE_URL", "").strip().rstrip("/"),
        arvan_model=os.getenv("SOORIN_ARVAN_MODEL", "GLM-5.2").strip(),
        arvan_api_key=os.getenv("SOORIN_ARVAN_API_KEY", "").strip(),
        arvan_auth_scheme=os.getenv("SOORIN_ARVAN_AUTH_SCHEME", "apikey").strip(),
        arvan_chat_path=os.getenv("SOORIN_ARVAN_CHAT_PATH", "/chat/completions").strip(),
        arvan_timeout_seconds=_int("SOORIN_ARVAN_TIMEOUT_SECONDS", 300),
        arvan_connect_timeout_seconds=_int("SOORIN_ARVAN_CONNECT_TIMEOUT_SECONDS", 30),
        arvan_max_tokens=_int("SOORIN_ARVAN_MAX_TOKENS", 8192),
        arvan_temperature=_float("SOORIN_ARVAN_TEMPERATURE", 0.2),
        arvan_top_p=_float("SOORIN_ARVAN_TOP_P", 0.9),
        llm_expose_reasoning=_bool("SOORIN_LLM_EXPOSE_REASONING", False),
        llm_log_raw_response=_bool("SOORIN_LLM_LOG_RAW_RESPONSE", False),
        chat_store_history=_bool("SOORIN_CHAT_STORE_HISTORY", True),
        chat_max_history_messages=_int("SOORIN_CHAT_MAX_HISTORY_MESSAGES", 20),
        conversation_max_messages=_int(
            "SOORIN_CONVERSATION_MAX_MESSAGES",
            _int("SOORIN_CHAT_MAX_HISTORY_MESSAGES", 10),
        ),
        system_prompt_path=os.getenv("SOORIN_SYSTEM_PROMPT_PATH", "app/prompts/system_prompt.md").strip(),
        product_api_base_url=os.getenv("SOORIN_PRODUCT_API_BASE_URL", "").strip().rstrip("/"),
        product_topology_path=os.getenv("SOORIN_PRODUCT_TOPOLOGY_PATH", "/zeek/connections/unique-ip-pairs").strip(),
        product_api_token=os.getenv("SOORIN_PRODUCT_API_TOKEN", "").strip(),
        product_hwid=os.getenv("SOORIN_PRODUCT_HWID", "").strip(),
        product_connect_timeout_seconds=_int("SOORIN_PRODUCT_CONNECT_TIMEOUT_SECONDS", 60),
        product_read_timeout_seconds=_int("SOORIN_PRODUCT_READ_TIMEOUT_SECONDS", 300),
        product_max_retries=_int("SOORIN_PRODUCT_MAX_RETRIES", 5),
        product_retry_backoff_seconds=_float("SOORIN_PRODUCT_RETRY_BACKOFF_SECONDS", 3.0),
        graph_raw_path=os.getenv("SOORIN_GRAPH_RAW_PATH", "data/raw/topology_raw.json").strip(),
        graph_pickle_path=os.getenv("SOORIN_GRAPH_PICKLE_PATH", "data/processed/topology_graph.pkl").strip(),
        graph_stats_path=os.getenv("SOORIN_GRAPH_STATS_PATH", "data/processed/topology_stats.json").strip(),
        graph_graphml_path=os.getenv("SOORIN_GRAPH_GRAPHML_PATH", "data/processed/topology_graph.graphml").strip(),
        graph_gexf_path=os.getenv("SOORIN_GRAPH_GEXF_PATH", "data/processed/topology_graph.gexf").strip(),
        graph_max_ui_nodes=_int("SOORIN_GRAPH_MAX_UI_NODES", 200),
        graph_default_min_degree=_int("SOORIN_GRAPH_DEFAULT_MIN_DEGREE", 1),
        graph_api_max_neighbors=_int("SOORIN_GRAPH_API_MAX_NEIGHBORS", 100),
        intent_router_enabled=_bool("SOORIN_INTENT_ROUTER_ENABLED", True),
        intent_router_timeout_seconds=_int("SOORIN_INTENT_ROUTER_TIMEOUT_SECONDS", 20),
        intent_router_min_confidence=_float("SOORIN_INTENT_ROUTER_MIN_CONFIDENCE", 0.65),
        copilot_human_trace_enabled=_bool("SOORIN_COPILOT_HUMAN_TRACE_ENABLED", True),
    )
