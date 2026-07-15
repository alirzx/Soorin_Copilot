"""Simple environment-based settings for the Copilot backend."""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = APP_DIR / ".env"
logger = logging.getLogger(__name__)


def _load_env_file(path: Path) -> bool:
    if not path.exists():
        return False

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)
    return True


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
    llm_connect_timeout_seconds: int
    chat_timeout_seconds: int
    arvan_max_tokens: int
    arvan_temperature: float
    arvan_top_p: float
    llm_max_transient_retries: int
    llm_retry_base_delay_seconds: float
    llm_retry_max_delay_seconds: float
    chat_max_tokens: int
    llm_context_window_tokens: int
    llm_reserved_output_tokens: int
    llm_context_safety_margin_tokens: int
    llm_expose_reasoning: bool
    llm_log_raw_response: bool
    chat_store_history: bool
    chat_max_history_messages: int
    conversation_max_messages: int
    conversation_recent_raw_messages: int
    conversation_summary_enabled: bool
    conversation_summary_trigger_tokens: int
    conversation_summary_max_tokens: int
    conversation_summary_temperature: float
    conversation_summary_timeout_seconds: int
    system_prompt_path: str
    product_api_base_url: str
    product_topology_path: str
    product_asset_detection_path: str
    product_login_path: str
    product_api_token: str
    product_username: str
    product_password: str
    product_captcha_bypass: str
    product_token_refresh_seconds: int
    product_hwid: str
    product_connect_timeout_seconds: int
    product_read_timeout_seconds: int
    product_max_retries: int
    product_retry_backoff_seconds: float
    detection_cache_enabled: bool
    detection_cache_ttl_seconds: int
    detection_stale_on_error: bool
    graph_raw_path: str
    graph_pickle_path: str
    graph_stats_path: str
    graph_graphml_path: str
    graph_gexf_path: str
    graph_max_ui_nodes: int
    graph_default_min_degree: int
    graph_api_max_neighbors: int
    graph_default_scope: str
    graph_one_hop_max_nodes: int
    graph_full_neighbors_hard_max: int
    graph_two_hop_max_nodes: int
    graph_max_edges: int
    graph_max_context_tokens: int
    graph_max_path_length: int
    graph_full_enumeration_max_peers: int
    graph_context_max_enumerated_nodes: int
    graph_context_max_enumerated_edges: int
    graph_comparison_max_peers_per_entity: int
    graph_comparison_max_shared_peers: int
    intent_router_enabled: bool
    intent_router_system_prompt_path: str
    intent_router_timeout_seconds: int
    intent_router_min_confidence: float
    intent_router_retry_enabled: bool
    intent_router_temperature: float
    intent_router_top_p: float
    intent_router_max_tokens: int
    intent_router_retry_max_tokens: int
    graph_auto_refresh_enabled: bool
    graph_refresh_interval_seconds: int
    graph_refresh_on_startup: bool
    graph_refresh_startup_delay_seconds: int
    graph_refresh_jitter_seconds: int
    graph_refresh_max_consecutive_failures: int
    graph_refresh_keep_raw_snapshots: int
    graph_refresh_keep_processed_snapshots: int
    graph_refresh_lock_timeout_seconds: int
    graph_refresh_min_nodes: int
    graph_refresh_min_edges: int
    graph_refresh_max_node_drop_ratio: float
    graph_refresh_max_edge_drop_ratio: float
    copilot_human_trace_enabled: bool


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_file_loaded = _load_env_file(ENV_PATH)
    settings = Settings(
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
        llm_connect_timeout_seconds=_int("SOORIN_LLM_CONNECT_TIMEOUT_SECONDS", 8),
        chat_timeout_seconds=_int("SOORIN_CHAT_TIMEOUT_SECONDS", 300),
        arvan_max_tokens=_int("SOORIN_ARVAN_MAX_TOKENS", 12288),
        arvan_temperature=_float("SOORIN_ARVAN_TEMPERATURE", 0.2),
        arvan_top_p=_float("SOORIN_ARVAN_TOP_P", 0.9),
        llm_max_transient_retries=_int("SOORIN_LLM_MAX_TRANSIENT_RETRIES", 1),
        llm_retry_base_delay_seconds=_float("SOORIN_LLM_RETRY_BASE_DELAY_SECONDS", 0.25),
        llm_retry_max_delay_seconds=_float("SOORIN_LLM_RETRY_MAX_DELAY_SECONDS", 1.0),
        chat_max_tokens=_int("SOORIN_CHAT_MAX_TOKENS", 4096),
        llm_context_window_tokens=_int("SOORIN_LLM_CONTEXT_WINDOW_TOKENS", 32768),
        llm_reserved_output_tokens=_int("SOORIN_LLM_RESERVED_OUTPUT_TOKENS", 12288),
        llm_context_safety_margin_tokens=_int("SOORIN_LLM_CONTEXT_SAFETY_MARGIN_TOKENS", 2048),
        llm_expose_reasoning=_bool("SOORIN_LLM_EXPOSE_REASONING", False),
        llm_log_raw_response=_bool("SOORIN_LLM_LOG_RAW_RESPONSE", False),
        chat_store_history=_bool("SOORIN_CHAT_STORE_HISTORY", True),
        chat_max_history_messages=_int("SOORIN_CHAT_MAX_HISTORY_MESSAGES", 20),
        conversation_max_messages=_int(
            "SOORIN_CONVERSATION_MAX_MESSAGES",
            _int("SOORIN_CHAT_MAX_HISTORY_MESSAGES", 10),
        ),
        conversation_recent_raw_messages=_int("SOORIN_CONVERSATION_RECENT_RAW_MESSAGES", 4),
        conversation_summary_enabled=_bool("SOORIN_CONVERSATION_SUMMARY_ENABLED", True),
        conversation_summary_trigger_tokens=_int("SOORIN_CONVERSATION_SUMMARY_TRIGGER_TOKENS", 1800),
        conversation_summary_max_tokens=_int("SOORIN_CONVERSATION_SUMMARY_MAX_TOKENS", 700),
        conversation_summary_temperature=_float("SOORIN_CONVERSATION_SUMMARY_TEMPERATURE", 0.0),
        conversation_summary_timeout_seconds=_int("SOORIN_CONVERSATION_SUMMARY_TIMEOUT_SECONDS", 30),
        system_prompt_path=os.getenv("SOORIN_SYSTEM_PROMPT_PATH", "app/prompts/system_prompt.md").strip(),
        product_api_base_url=os.getenv("SOORIN_PRODUCT_API_BASE_URL", "").strip().rstrip("/"),
        product_topology_path=os.getenv("SOORIN_PRODUCT_TOPOLOGY_PATH", "/zeek/connections/unique-ip-pairs").strip(),
        product_asset_detection_path=os.getenv("SOORIN_PRODUCT_ASSET_DETECTION_PATH", "/asset-detection/test/{ip}").strip(),
        product_login_path=os.getenv("SOORIN_PRODUCT_LOGIN_PATH", "/auth/login").strip(),
        product_api_token=os.getenv("SOORIN_PRODUCT_API_TOKEN", "").strip(),
        product_username=os.getenv("SOORIN_PRODUCT_USERNAME", "").strip(),
        product_password=os.getenv("SOORIN_PRODUCT_PASSWORD", "").strip(),
        product_captcha_bypass=os.getenv("SOORIN_PRODUCT_CAPTCHA_BYPASS", "").strip(),
        product_token_refresh_seconds=_int("SOORIN_PRODUCT_TOKEN_REFRESH_SECONDS", 600),
        product_hwid=os.getenv("SOORIN_PRODUCT_HWID", "").strip(),
        product_connect_timeout_seconds=_int("SOORIN_PRODUCT_CONNECT_TIMEOUT_SECONDS", 60),
        product_read_timeout_seconds=_int("SOORIN_PRODUCT_READ_TIMEOUT_SECONDS", 300),
        product_max_retries=_int("SOORIN_PRODUCT_MAX_RETRIES", 5),
        product_retry_backoff_seconds=_float("SOORIN_PRODUCT_RETRY_BACKOFF_SECONDS", 3.0),
        detection_cache_enabled=_bool("SOORIN_DETECTION_CACHE_ENABLED", True),
        detection_cache_ttl_seconds=_int("SOORIN_DETECTION_CACHE_TTL_SECONDS", 300),
        detection_stale_on_error=_bool("SOORIN_DETECTION_STALE_ON_ERROR", True),
        graph_raw_path=os.getenv("SOORIN_GRAPH_RAW_PATH", "data/raw/topology_raw.json").strip(),
        graph_pickle_path=os.getenv("SOORIN_GRAPH_PICKLE_PATH", "data/processed/topology_graph.pkl").strip(),
        graph_stats_path=os.getenv("SOORIN_GRAPH_STATS_PATH", "data/processed/topology_stats.json").strip(),
        graph_graphml_path=os.getenv("SOORIN_GRAPH_GRAPHML_PATH", "data/processed/topology_graph.graphml").strip(),
        graph_gexf_path=os.getenv("SOORIN_GRAPH_GEXF_PATH", "data/processed/topology_graph.gexf").strip(),
        graph_max_ui_nodes=_int("SOORIN_GRAPH_MAX_UI_NODES", 1000),
        graph_default_min_degree=_int("SOORIN_GRAPH_DEFAULT_MIN_DEGREE", 1),
        graph_api_max_neighbors=_int("SOORIN_GRAPH_API_MAX_NEIGHBORS", 1000),
        graph_default_scope=os.getenv("SOORIN_GRAPH_DEFAULT_SCOPE", "node_summary").strip(),
        graph_one_hop_max_nodes=_int("SOORIN_GRAPH_ONE_HOP_MAX_NODES", 500),
        graph_full_neighbors_hard_max=_int("SOORIN_GRAPH_FULL_NEIGHBORS_HARD_MAX", 5000),
        graph_two_hop_max_nodes=_int("SOORIN_GRAPH_TWO_HOP_MAX_NODES", 1000),
        graph_max_edges=_int("SOORIN_GRAPH_MAX_EDGES", 5000),
        graph_max_context_tokens=_int("SOORIN_GRAPH_MAX_CONTEXT_TOKENS", 8000),
        graph_max_path_length=_int("SOORIN_GRAPH_MAX_PATH_LENGTH", 24),
        graph_full_enumeration_max_peers=_int("SOORIN_GRAPH_FULL_ENUMERATION_MAX_PEERS", 100),
        graph_context_max_enumerated_nodes=_int("SOORIN_GRAPH_CONTEXT_MAX_ENUMERATED_NODES", 250),
        graph_context_max_enumerated_edges=_int("SOORIN_GRAPH_CONTEXT_MAX_ENUMERATED_EDGES", 500),
        graph_comparison_max_peers_per_entity=_int("SOORIN_GRAPH_COMPARISON_MAX_PEERS_PER_ENTITY", 100),
        graph_comparison_max_shared_peers=_int("SOORIN_GRAPH_COMPARISON_MAX_SHARED_PEERS", 100),
        intent_router_enabled=_bool("SOORIN_INTENT_ROUTER_ENABLED", True),
        intent_router_system_prompt_path=os.getenv(
            "SOORIN_INTENT_ROUTER_SYSTEM_PROMPT_PATH",
            "app/prompts/intent_router_system_prompt.md",
        ).strip(),
        intent_router_timeout_seconds=_int("SOORIN_INTENT_ROUTER_TIMEOUT_SECONDS", 15),
        intent_router_min_confidence=_float("SOORIN_INTENT_ROUTER_MIN_CONFIDENCE", 0.65),
        intent_router_retry_enabled=_bool("SOORIN_INTENT_ROUTER_RETRY_ENABLED", True),
        intent_router_temperature=_float("SOORIN_INTENT_ROUTER_TEMPERATURE", 0.0),
        intent_router_top_p=_float("SOORIN_INTENT_ROUTER_TOP_P", 0.1),
        intent_router_max_tokens=_int("SOORIN_INTENT_ROUTER_MAX_TOKENS", 384),
        intent_router_retry_max_tokens=_int("SOORIN_INTENT_ROUTER_RETRY_MAX_TOKENS", 640),
        graph_auto_refresh_enabled=_bool("SOORIN_GRAPH_AUTO_REFRESH_ENABLED", True),
        graph_refresh_interval_seconds=_int("SOORIN_GRAPH_REFRESH_INTERVAL_SECONDS", 900),
        graph_refresh_on_startup=_bool("SOORIN_GRAPH_REFRESH_ON_STARTUP", True),
        graph_refresh_startup_delay_seconds=_int("SOORIN_GRAPH_REFRESH_STARTUP_DELAY_SECONDS", 5),
        graph_refresh_jitter_seconds=_int("SOORIN_GRAPH_REFRESH_JITTER_SECONDS", 30),
        graph_refresh_max_consecutive_failures=_int("SOORIN_GRAPH_REFRESH_MAX_CONSECUTIVE_FAILURES", 5),
        graph_refresh_keep_raw_snapshots=_int("SOORIN_GRAPH_REFRESH_KEEP_RAW_SNAPSHOTS", 5),
        graph_refresh_keep_processed_snapshots=_int("SOORIN_GRAPH_REFRESH_KEEP_PROCESSED_SNAPSHOTS", 3),
        graph_refresh_lock_timeout_seconds=_int("SOORIN_GRAPH_REFRESH_LOCK_TIMEOUT_SECONDS", 60),
        graph_refresh_min_nodes=_int("SOORIN_GRAPH_REFRESH_MIN_NODES", 1),
        graph_refresh_min_edges=_int("SOORIN_GRAPH_REFRESH_MIN_EDGES", 0),
        graph_refresh_max_node_drop_ratio=_float("SOORIN_GRAPH_REFRESH_MAX_NODE_DROP_RATIO", 0.80),
        graph_refresh_max_edge_drop_ratio=_float("SOORIN_GRAPH_REFRESH_MAX_EDGE_DROP_RATIO", 0.90),
        copilot_human_trace_enabled=_bool("SOORIN_COPILOT_HUMAN_TRACE_ENABLED", True),
    )
    logger.info(
        "event=settings_loaded env_file_path=%s env_file_loaded=%s product_base_url_configured=%s product_token_present=%s product_hwid_present=%s product_username_present=%s product_password_present=%s product_captcha_bypass_present=%s",
        ENV_PATH,
        env_file_loaded,
        bool(settings.product_api_base_url),
        bool(settings.product_api_token),
        bool(settings.product_hwid),
        bool(settings.product_username),
        bool(settings.product_password),
        bool(settings.product_captcha_bypass),
    )
    return settings
