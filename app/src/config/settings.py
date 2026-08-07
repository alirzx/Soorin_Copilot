"""Simple environment-based settings for the Copilot backend."""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src.config.llm_deployments import (
    ArvanDeploymentConfig,
    LLMDeploymentName,
    VALID_LLM_DEPLOYMENTS,
)


APP_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = APP_DIR / ".env"
logger = logging.getLogger(__name__)

DEFAULT_RAG_EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"
DEFAULT_RAG_EMBEDDING_DIMENSION = 768
DEFAULT_RAG_COLLECTION = "soorin_soc_knowledge_bge_base_v1"
LEGACY_RAG_EMBEDDING_MODELS = {"ehsanaghaei/securebert"}
LEGACY_RAG_COLLECTIONS = {"soorin_soc_knowledge"}


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


def _optional_float(name: str) -> float | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return float(value)


def _choice(name: str, default: str, choices: set[str]) -> str:
    value = (os.getenv(name) or default).split(" #", 1)[0].strip().lower()
    if value not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(choices))}.")
    return value


def _deployment_name(name: str, default: LLMDeploymentName = "kimi") -> LLMDeploymentName:
    value = (os.getenv(name) or default).strip().lower()
    if value not in VALID_LLM_DEPLOYMENTS:
        valid = ", ".join(VALID_LLM_DEPLOYMENTS)
        raise ValueError(f"Invalid LLM deployment alias for {name}. Valid aliases: {valid}")
    return value  # type: ignore[return-value]
@dataclass(frozen=True)
class Settings:
    api_host: str
    api_port: int
    api_reload: bool
    log_level: str
    log_format: str
    log_color: str
    log_file_enabled: bool
    log_file_path: str
    log_file_level: str
    log_file_max_bytes: int
    log_file_backup_count: int
    api_base_url: str
    api_timeout_seconds: int
    streamlit_server_port: int
    copilot_api_key: str
    llm_enabled: bool
    llm_provider: str
    intent_router_deployment: LLMDeploymentName
    chat_deployment: LLMDeploymentName
    planner_enabled: bool
    planner_deployment: LLMDeploymentName
    planner_repair_enabled: bool
    planner_system_prompt_path: str
    agent_max_supplemental_retrievals: int
    agent_max_capability_calls: int
    agent_max_entities: int
    agent_max_graph_depth: int
    agent_executor_max_concurrency: int
    agent_request_timeout_seconds: float
    kimi_base_url: str
    kimi_chat_path: str
    kimi_model: str
    kimi_api_key: str
    kimi_auth_scheme: str
    kimi_connect_timeout_seconds: int
    kimi_router_timeout_seconds: int
    kimi_chat_timeout_seconds: int
    kimi_max_tokens: int
    kimi_router_max_tokens: int
    kimi_router_retry_max_tokens: int
    kimi_chat_max_tokens: int
    kimi_router_temperature: float | None
    kimi_router_top_p: float | None
    kimi_chat_temperature: float | None
    kimi_chat_top_p: float | None
    kimi_supports_temperature: bool
    kimi_supports_top_p: bool
    glm_base_url: str
    glm_chat_path: str
    glm_model: str
    glm_api_key: str
    glm_auth_scheme: str
    glm_connect_timeout_seconds: int
    glm_router_timeout_seconds: int
    glm_chat_timeout_seconds: int
    glm_max_tokens: int
    glm_router_max_tokens: int
    glm_router_retry_max_tokens: int
    glm_chat_max_tokens: int
    glm_router_temperature: float | None
    glm_router_top_p: float | None
    glm_chat_temperature: float | None
    glm_chat_top_p: float | None
    glm_supports_temperature: bool
    glm_supports_top_p: bool
    gpt55_base_url: str
    gpt55_chat_path: str
    gpt55_model: str
    gpt55_api_key: str
    gpt55_auth_scheme: str
    gpt55_connect_timeout_seconds: int
    gpt55_router_timeout_seconds: int
    gpt55_chat_timeout_seconds: int
    gpt55_max_tokens: int
    gpt55_router_max_tokens: int
    gpt55_router_retry_max_tokens: int
    gpt55_chat_max_tokens: int
    gpt55_router_temperature: float | None
    gpt55_router_top_p: float | None
    gpt55_chat_temperature: float | None
    gpt55_chat_top_p: float | None
    gpt55_supports_temperature: bool
    gpt55_supports_top_p: bool
    llm_max_transient_retries: int
    llm_retry_base_delay_seconds: float
    llm_retry_max_delay_seconds: float
    llm_context_window_tokens: int
    llm_reserved_output_tokens: int
    llm_context_safety_margin_tokens: int
    llm_token_estimate_multiplier: float
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
    durable_working_memory_enabled: bool
    memory_relevant_turn_limit: int
    memory_relevant_turn_token_budget: int
    memory_episode_retention_limit: int
    memory_episode_context_limit: int
    memory_episode_context_token_budget: int
    memory_context_token_budget: int
    long_term_memory_enabled: bool
    long_term_memory_backend: str
    memory_vector_index_enabled: bool
    memory_qdrant_collection: str
    memory_retrieval_candidate_k: int
    memory_retrieval_top_k: int
    memory_min_score: float
    memory_rerank_enabled: bool
    memory_rerank_model: str
    memory_rerank_timeout_seconds: float
    memory_context_long_term_token_budget: int
    local_product_simulation_enabled: bool
    streamlit_auth_backend: str
    local_test_user_creation_enabled: bool
    thread_state_backend: str
    local_sqlite_path: str
    langgraph_checkpoint_backend: str
    system_prompt_path: str
    product_api_base_url: str
    product_topology_path: str
    product_asset_detection_path: str
    product_asset_profile_path: str
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
    rag_enabled: bool
    rag_source_root: str
    rag_backend: str
    rag_collection: str
    rag_top_k: int
    rag_score_threshold: float
    rag_qdrant_mode: str
    rag_qdrant_url: str
    rag_qdrant_path: str
    rag_qdrant_api_key: str
    rag_qdrant_timeout_seconds: float
    rag_embedding_model: str
    rag_embedding_dimension: int
    rag_embedding_local_files_only: bool
    rag_embedding_cache_dir: str
    rag_embedding_revision: str
    rag_distance: str
    rag_max_context_tokens: int
    rag_chunk_size_chars: int
    rag_chunk_overlap_chars: int
    rag_upsert_batch_size: int
    intent_router_enabled: bool
    intent_router_system_prompt_path: str
    intent_router_min_confidence: float
    intent_router_retry_enabled: bool
    graph_auto_refresh_enabled: bool
    graph_refresh_interval_seconds: int
    graph_refresh_on_startup: bool
    graph_refresh_startup_delay_seconds: int
    graph_refresh_jitter_seconds: int
    graph_refresh_max_consecutive_failures: int
    graph_refresh_keep_raw_snapshots: int
    graph_refresh_keep_processed_snapshots: int
    graph_snapshot_ttl_hours: int
    graph_optional_exports_enabled: bool
    graph_refresh_lock_timeout_seconds: int
    graph_refresh_min_nodes: int
    graph_refresh_min_edges: int
    graph_refresh_max_node_drop_ratio: float
    graph_refresh_max_edge_drop_ratio: float
    copilot_human_trace_enabled: bool
    copilot_human_trace_detail: str
    evidence_snapshot_enabled: bool
    evidence_snapshot_mode: str
    evidence_snapshot_root: str
    evidence_snapshot_ttl_hours: int
    evidence_snapshot_max_requests: int
    evidence_snapshot_max_total_bytes: int
    evidence_snapshot_max_bytes: int
    llm_usage_reporting_enabled: bool
    llm_usage_reporting_url: str

    def deployment(self, name: LLMDeploymentName) -> ArvanDeploymentConfig:
        """Build any named OpenAI-compatible deployment through one contract."""
        if name == "kimi":
            return ArvanDeploymentConfig(
                name="kimi",
                base_url=self.kimi_base_url,
                chat_path=self.kimi_chat_path,
                model=self.kimi_model,
                api_key=self.kimi_api_key,
                auth_scheme=self.kimi_auth_scheme,
                connect_timeout_seconds=self.kimi_connect_timeout_seconds,
                maximum_completion_tokens=self.kimi_max_tokens,
                router_read_timeout_seconds=self.kimi_router_timeout_seconds,
                router_max_tokens=self.kimi_router_max_tokens,
                router_repair_max_tokens=self.kimi_router_retry_max_tokens,
                chat_read_timeout_seconds=self.kimi_chat_timeout_seconds,
                chat_max_tokens=self.kimi_chat_max_tokens,
                router_temperature=self.kimi_router_temperature,
                router_top_p=self.kimi_router_top_p,
                chat_temperature=self.kimi_chat_temperature,
                chat_top_p=self.kimi_chat_top_p,
                supports_temperature=self.kimi_supports_temperature,
                supports_top_p=self.kimi_supports_top_p,
                provider_type=self.llm_provider,
            )
        if name == "glm":
            return ArvanDeploymentConfig(
                name="glm",
                base_url=self.glm_base_url,
                chat_path=self.glm_chat_path,
                model=self.glm_model,
                api_key=self.glm_api_key,
                auth_scheme=self.glm_auth_scheme,
                connect_timeout_seconds=self.glm_connect_timeout_seconds,
                maximum_completion_tokens=self.glm_max_tokens,
                router_read_timeout_seconds=self.glm_router_timeout_seconds,
                router_max_tokens=self.glm_router_max_tokens,
                router_repair_max_tokens=self.glm_router_retry_max_tokens,
                chat_read_timeout_seconds=self.glm_chat_timeout_seconds,
                chat_max_tokens=self.glm_chat_max_tokens,
                router_temperature=self.glm_router_temperature,
                router_top_p=self.glm_router_top_p,
                chat_temperature=self.glm_chat_temperature,
                chat_top_p=self.glm_chat_top_p,
                supports_temperature=self.glm_supports_temperature,
                supports_top_p=self.glm_supports_top_p,
                provider_type=self.llm_provider,
            )
        if name == "gpt55":
            return ArvanDeploymentConfig(
                name="gpt55",
                base_url=self.gpt55_base_url,
                chat_path=self.gpt55_chat_path,
                model=self.gpt55_model,
                api_key=self.gpt55_api_key,
                auth_scheme=self.gpt55_auth_scheme,
                connect_timeout_seconds=self.gpt55_connect_timeout_seconds,
                maximum_completion_tokens=self.gpt55_max_tokens,
                router_read_timeout_seconds=self.gpt55_router_timeout_seconds,
                router_max_tokens=self.gpt55_router_max_tokens,
                router_repair_max_tokens=self.gpt55_router_retry_max_tokens,
                chat_read_timeout_seconds=self.gpt55_chat_timeout_seconds,
                chat_max_tokens=self.gpt55_chat_max_tokens,
                router_temperature=self.gpt55_router_temperature,
                router_top_p=self.gpt55_router_top_p,
                chat_temperature=self.gpt55_chat_temperature,
                chat_top_p=self.gpt55_chat_top_p,
                supports_temperature=self.gpt55_supports_temperature,
                supports_top_p=self.gpt55_supports_top_p,
                provider_type=self.llm_provider,
            )
        valid = ", ".join(VALID_LLM_DEPLOYMENTS)
        raise ValueError(f"Invalid LLM deployment alias. Valid aliases: {valid}")

    def deployment_for_purpose(self, purpose: str) -> ArvanDeploymentConfig:
        if purpose == "chat":
            alias = self.chat_deployment
        elif purpose in {"planner", "planner_repair"}:
            alias = self.planner_deployment
        else:
            alias = self.intent_router_deployment
        return self.deployment(alias)

    def validate_selected_llm_deployments(self) -> None:
        """Fail startup safely when an enabled selected deployment has no endpoint."""
        if not self.llm_enabled or self.llm_provider != "arvan":
            return
        aliases = [self.intent_router_deployment, self.chat_deployment]
        if self.planner_enabled:
            aliases.append(self.planner_deployment)
        selected = dict.fromkeys(aliases)
        missing = [alias for alias in selected if not self.deployment(alias).base_url]
        if missing:
            raise ValueError(
                "Selected LLM deployment base URL is not configured for: "
                + ", ".join(missing)
            )

    def validate_product_paths(self) -> None:
        """Validate asset path templates without exposing configured URLs."""
        if self.product_asset_profile_path.count("{ip}") != 1 or ".." in self.product_asset_profile_path:
            raise ValueError("SOORIN_PRODUCT_ASSET_PROFILE_PATH must contain exactly one safe {ip} placeholder.")

    def validate_rag_qdrant_configuration(self) -> None:
        """Validate Qdrant settings only when RAG actually uses Qdrant."""
        if not self.rag_enabled or self.rag_backend != "qdrant":
            return

        if self.rag_qdrant_mode not in {"server", "local"}:
            raise ValueError("SOORIN_RAG_QDRANT_MODE must be either server or local.")

        if self.rag_qdrant_mode == "local" and not self.rag_qdrant_path:
            raise ValueError("SOORIN_RAG_QDRANT_PATH is required when SOORIN_RAG_QDRANT_MODE=local.")

        if self.rag_qdrant_mode == "server" and not self.rag_qdrant_url:
            raise ValueError("SOORIN_RAG_QDRANT_URL is required when SOORIN_RAG_QDRANT_MODE=server.")

    def validate_rag_embedding_configuration(self) -> None:
        """Prevent silent reuse of incompatible embedding collections."""
        if not self.rag_enabled:
            return

        if self.rag_embedding_model.lower() in LEGACY_RAG_EMBEDDING_MODELS:
            raise ValueError("SecureBERT is no longer supported for Soorin RAG embeddings.")

        if self.rag_embedding_model == DEFAULT_RAG_EMBEDDING_MODEL:
            if self.rag_embedding_dimension != DEFAULT_RAG_EMBEDDING_DIMENSION:
                raise ValueError("BAAI/bge-base-en-v1.5 requires SOORIN_RAG_EMBEDDING_DIMENSION=768.")

            if self.rag_collection in LEGACY_RAG_COLLECTIONS:
                raise ValueError(
                    "BAAI/bge-base-en-v1.5 must not reuse a legacy RAG collection. "
                    f"Use SOORIN_RAG_COLLECTION={DEFAULT_RAG_COLLECTION} or another freshly indexed collection."
                )

    def validate_observability_configuration(self) -> None:
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.log_level.upper() not in valid_levels:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL.")
        if self.log_file_level.upper() not in valid_levels:
            raise ValueError(
                "SOORIN_LOG_FILE_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL."
            )
        if self.log_format not in {"console", "json"}:
            raise ValueError("SOORIN_LOG_FORMAT must be console or json.")
        if self.log_color not in {"auto", "always", "never"}:
            raise ValueError("SOORIN_LOG_COLOR must be auto, always, or never.")
        if self.copilot_human_trace_detail not in {"summary", "detailed"}:
            raise ValueError("SOORIN_HUMAN_TRACE_DETAIL must be summary or detailed.")
        if self.evidence_snapshot_mode not in {"none", "metadata", "summary", "redacted"}:
            raise ValueError(
                "SOORIN_EVIDENCE_SNAPSHOT_MODE must be none, metadata, summary, or redacted."
            )

    def validate_local_persistence_configuration(self) -> None:
        """Validate only explicitly enabled local-development persistence."""
        sqlite_required = (
            self.local_product_simulation_enabled
            or self.thread_state_backend == "sqlite"
            or (self.long_term_memory_enabled and self.long_term_memory_backend == "sqlite")
        )
        if sqlite_required and not self.local_sqlite_path:
            raise ValueError(
                "SOORIN_LOCAL_SQLITE_PATH is required when local SQLite persistence is enabled."
            )
        if (
            self.streamlit_auth_backend == "local_simulation"
            and not self.local_product_simulation_enabled
        ):
            raise ValueError(
                "SOORIN_STREAMLIT_AUTH_BACKEND=local_simulation requires "
                "SOORIN_LOCAL_PRODUCT_SIMULATION_ENABLED=true."
            )

    def validate_long_term_memory_configuration(self) -> None:
        if not self.long_term_memory_enabled:
            return
        if self.long_term_memory_backend != "sqlite":
            raise ValueError("Only the local SQLite long-term memory backend is currently available.")
        if self.memory_vector_index_enabled:
            if not self.memory_qdrant_collection:
                raise ValueError("SOORIN_MEMORY_QDRANT_COLLECTION must not be blank.")
            if self.memory_qdrant_collection == self.rag_collection:
                raise ValueError("Long-term memory and SOC knowledge require separate Qdrant collections.")
            if self.rag_qdrant_mode == "local" and not self.rag_qdrant_path:
                raise ValueError("Local memory indexing requires SOORIN_RAG_QDRANT_PATH.")
            if self.rag_qdrant_mode == "server" and not self.rag_qdrant_url:
                raise ValueError("Server memory indexing requires SOORIN_RAG_QDRANT_URL.")
            if self.rag_embedding_model.lower() in LEGACY_RAG_EMBEDDING_MODELS:
                raise ValueError("SecureBERT is not supported for long-term memory embeddings.")
            if (
                self.rag_embedding_model == DEFAULT_RAG_EMBEDDING_MODEL
                and self.rag_embedding_dimension != DEFAULT_RAG_EMBEDDING_DIMENSION
            ):
                raise ValueError("BAAI/bge-base-en-v1.5 long-term memory embeddings require dimension 768.")
        if self.memory_rerank_enabled and not self.memory_rerank_model:
            raise ValueError("SOORIN_MEMORY_RERANK_MODEL is required when reranking is enabled.")

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_file_loaded = _load_env_file(ENV_PATH)
    settings = Settings(
        api_host=os.getenv("API_HOST", "0.0.0.0"),
        api_port=_int("API_PORT", 6998),
        api_reload=_bool("API_RELOAD", True),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_format=os.getenv("SOORIN_LOG_FORMAT", "console").strip().lower(),
        log_color=os.getenv("SOORIN_LOG_COLOR", "auto").strip().lower(),
        log_file_enabled=_bool("SOORIN_LOG_FILE_ENABLED", True),
        log_file_path=os.getenv(
            "SOORIN_LOG_FILE_PATH", "data/runtime/logs/soorin-copilot.log"
        ).strip(),
        log_file_level=os.getenv("SOORIN_LOG_FILE_LEVEL", "INFO").strip().upper(),
        log_file_max_bytes=max(1024, _int("SOORIN_LOG_FILE_MAX_BYTES", 20971520)),
        log_file_backup_count=max(0, _int("SOORIN_LOG_FILE_BACKUP_COUNT", 10)),
        api_base_url=os.getenv("SOORIN_API_BASE_URL", "http://127.0.0.1:6998").strip().rstrip("/"),
        api_timeout_seconds=_int("SOORIN_API_TIMEOUT_SECONDS", 120),
        streamlit_server_port=_int("STREAMLIT_SERVER_PORT", 8503),
        copilot_api_key=os.getenv("SOORIN_COPILOT_API_KEY", "").strip(),
        llm_enabled=_bool("SOORIN_LLM_ENABLED", True),
        llm_provider=os.getenv("SOORIN_LLM_PROVIDER", "arvan").strip().lower(),
        intent_router_deployment=_deployment_name("SOORIN_INTENT_ROUTER_DEPLOYMENT"),
        chat_deployment=_deployment_name("SOORIN_CHAT_DEPLOYMENT"),
        planner_enabled=_bool("SOORIN_PLANNER_ENABLED", False),
        planner_deployment=_deployment_name("SOORIN_PLANNER_DEPLOYMENT", "glm"),
        planner_repair_enabled=_bool("SOORIN_PLANNER_REPAIR_ENABLED", True),
        planner_system_prompt_path=os.getenv(
            "SOORIN_PLANNER_SYSTEM_PROMPT_PATH",
            "app/prompts/planner_system_prompt.md",
        ).strip(),
        agent_max_supplemental_retrievals=max(0, min(1, _int("SOORIN_AGENT_MAX_SUPPLEMENTAL_RETRIEVALS", 1))),
        agent_max_capability_calls=max(1, min(6, _int("SOORIN_AGENT_MAX_CAPABILITY_CALLS", 6))),
        agent_max_entities=max(1, min(2, _int("SOORIN_AGENT_MAX_ENTITIES", 2))),
        agent_max_graph_depth=max(0, min(2, _int("SOORIN_AGENT_MAX_GRAPH_DEPTH", 2))),
        agent_executor_max_concurrency=max(1, min(4, _int("SOORIN_AGENT_EXECUTOR_MAX_CONCURRENCY", 4))),
        agent_request_timeout_seconds=max(1.0, _float("SOORIN_AGENT_REQUEST_TIMEOUT_SECONDS", 120.0)),
        kimi_base_url=os.getenv("SOORIN_LLM_KIMI_BASE_URL", "").strip().rstrip("/"),
        kimi_chat_path=os.getenv("SOORIN_LLM_KIMI_CHAT_PATH", "/chat/completions").strip(),
        kimi_model=os.getenv("SOORIN_LLM_KIMI_MODEL", "kimi-k3").strip(),
        kimi_api_key=os.getenv("SOORIN_LLM_KIMI_API_KEY", "").strip(),
        kimi_auth_scheme=os.getenv("SOORIN_LLM_KIMI_AUTH_SCHEME", "apikey").strip(),
        kimi_connect_timeout_seconds=_int("SOORIN_LLM_KIMI_CONNECT_TIMEOUT_SECONDS", 8),
        kimi_router_timeout_seconds=_int("SOORIN_LLM_KIMI_ROUTER_TIMEOUT_SECONDS", 30),
        kimi_chat_timeout_seconds=_int("SOORIN_LLM_KIMI_CHAT_TIMEOUT_SECONDS", 360),
        kimi_max_tokens=_int("SOORIN_LLM_KIMI_MAX_TOKENS", 12288),
        kimi_router_max_tokens=_int("SOORIN_LLM_KIMI_ROUTER_MAX_TOKENS", 924),
        kimi_router_retry_max_tokens=_int("SOORIN_LLM_KIMI_ROUTER_RETRY_MAX_TOKENS", 1284),
        kimi_chat_max_tokens=_int("SOORIN_LLM_KIMI_CHAT_MAX_TOKENS", 12288),
        kimi_router_temperature=_optional_float("SOORIN_LLM_KIMI_ROUTER_TEMPERATURE"),
        kimi_router_top_p=_optional_float("SOORIN_LLM_KIMI_ROUTER_TOP_P"),
        kimi_chat_temperature=_optional_float("SOORIN_LLM_KIMI_CHAT_TEMPERATURE"),
        kimi_chat_top_p=_optional_float("SOORIN_LLM_KIMI_CHAT_TOP_P"),
        kimi_supports_temperature=_bool("SOORIN_LLM_KIMI_SUPPORTS_TEMPERATURE", False),
        kimi_supports_top_p=_bool("SOORIN_LLM_KIMI_SUPPORTS_TOP_P", False),
        glm_base_url=os.getenv("SOORIN_LLM_GLM_BASE_URL", "").strip().rstrip("/"),
        glm_chat_path=os.getenv("SOORIN_LLM_GLM_CHAT_PATH", "/chat/completions").strip(),
        glm_model=os.getenv("SOORIN_LLM_GLM_MODEL", "GLM-5.2").strip(),
        glm_api_key=os.getenv("SOORIN_LLM_GLM_API_KEY", "").strip(),
        glm_auth_scheme=os.getenv("SOORIN_LLM_GLM_AUTH_SCHEME", "apikey").strip(),
        glm_connect_timeout_seconds=_int("SOORIN_LLM_GLM_CONNECT_TIMEOUT_SECONDS", 8),
        glm_router_timeout_seconds=_int("SOORIN_LLM_GLM_ROUTER_TIMEOUT_SECONDS", 15),
        glm_chat_timeout_seconds=_int("SOORIN_LLM_GLM_CHAT_TIMEOUT_SECONDS", 300),
        glm_max_tokens=_int("SOORIN_LLM_GLM_MAX_TOKENS", 12288),
        glm_router_max_tokens=_int("SOORIN_LLM_GLM_ROUTER_MAX_TOKENS", 384),
        glm_router_retry_max_tokens=_int("SOORIN_LLM_GLM_ROUTER_RETRY_MAX_TOKENS", 640),
        glm_chat_max_tokens=_int("SOORIN_LLM_GLM_CHAT_MAX_TOKENS", 4096),
        glm_router_temperature=_optional_float("SOORIN_LLM_GLM_ROUTER_TEMPERATURE"),
        glm_router_top_p=_optional_float("SOORIN_LLM_GLM_ROUTER_TOP_P"),
        glm_chat_temperature=_optional_float("SOORIN_LLM_GLM_CHAT_TEMPERATURE"),
        glm_chat_top_p=_optional_float("SOORIN_LLM_GLM_CHAT_TOP_P"),
        glm_supports_temperature=_bool("SOORIN_LLM_GLM_SUPPORTS_TEMPERATURE", True),
        glm_supports_top_p=_bool("SOORIN_LLM_GLM_SUPPORTS_TOP_P", True),
        gpt55_base_url=os.getenv("SOORIN_LLM_GPT55_BASE_URL", "").strip().rstrip("/"),
        gpt55_chat_path=os.getenv("SOORIN_LLM_GPT55_CHAT_PATH", "/chat/completions").strip(),
        gpt55_model=os.getenv("SOORIN_LLM_GPT55_MODEL", "GPT-5.5").strip(),
        gpt55_api_key=os.getenv("SOORIN_LLM_GPT55_API_KEY", "").strip(),
        gpt55_auth_scheme=os.getenv("SOORIN_LLM_GPT55_AUTH_SCHEME", "apikey").strip(),
        gpt55_connect_timeout_seconds=_int(
            "SOORIN_LLM_GPT55_CONNECT_TIMEOUT_SECONDS",
            8,
        ),
        gpt55_router_timeout_seconds=_int(
            "SOORIN_LLM_GPT55_ROUTER_TIMEOUT_SECONDS",
            30,
        ),
        gpt55_chat_timeout_seconds=_int(
            "SOORIN_LLM_GPT55_CHAT_TIMEOUT_SECONDS",
            360,
        ),
        gpt55_max_tokens=_int(
            "SOORIN_LLM_GPT55_MAX_TOKENS",
            12288,
        ),
        gpt55_router_max_tokens=_int(
            "SOORIN_LLM_GPT55_ROUTER_MAX_TOKENS",
            924,
        ),
        gpt55_router_retry_max_tokens=_int(
            "SOORIN_LLM_GPT55_ROUTER_RETRY_MAX_TOKENS",
            1284,
        ),
        gpt55_chat_max_tokens=_int(
            "SOORIN_LLM_GPT55_CHAT_MAX_TOKENS",
            12288,
        ),
        gpt55_router_temperature=_optional_float("SOORIN_LLM_GPT55_ROUTER_TEMPERATURE"),
        gpt55_router_top_p=_optional_float("SOORIN_LLM_GPT55_ROUTER_TOP_P"),
        gpt55_chat_temperature=_optional_float("SOORIN_LLM_GPT55_CHAT_TEMPERATURE"),
        gpt55_chat_top_p=_optional_float("SOORIN_LLM_GPT55_CHAT_TOP_P"),
        gpt55_supports_temperature=_bool("SOORIN_LLM_GPT55_SUPPORTS_TEMPERATURE", False),
        gpt55_supports_top_p=_bool("SOORIN_LLM_GPT55_SUPPORTS_TOP_P", False),
        llm_max_transient_retries=_int("SOORIN_LLM_MAX_TRANSIENT_RETRIES", 1),
        llm_retry_base_delay_seconds=_float("SOORIN_LLM_RETRY_BASE_DELAY_SECONDS", 0.25),
        llm_retry_max_delay_seconds=_float("SOORIN_LLM_RETRY_MAX_DELAY_SECONDS", 1.0),
        llm_context_window_tokens=_int("SOORIN_LLM_CONTEXT_WINDOW_TOKENS", 32768),
        llm_reserved_output_tokens=_int("SOORIN_LLM_RESERVED_OUTPUT_TOKENS", 12288),
        llm_context_safety_margin_tokens=_int("SOORIN_LLM_CONTEXT_SAFETY_MARGIN_TOKENS", 2048),
        llm_token_estimate_multiplier=max(1.0, _float("SOORIN_LLM_TOKEN_ESTIMATE_MULTIPLIER", 1.35)),
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
        durable_working_memory_enabled=_bool("SOORIN_DURABLE_WORKING_MEMORY_ENABLED", True),
        memory_relevant_turn_limit=max(0, _int("SOORIN_MEMORY_RELEVANT_TURN_LIMIT", 4)),
        memory_relevant_turn_token_budget=max(
            0, _int("SOORIN_MEMORY_RELEVANT_TURN_TOKEN_BUDGET", 900)
        ),
        memory_episode_retention_limit=max(
            1, _int("SOORIN_MEMORY_EPISODE_RETENTION_LIMIT", 12)
        ),
        memory_episode_context_limit=max(
            0, _int("SOORIN_MEMORY_EPISODE_CONTEXT_LIMIT", 2)
        ),
        memory_episode_context_token_budget=max(
            0, _int("SOORIN_MEMORY_EPISODE_CONTEXT_TOKEN_BUDGET", 300)
        ),
        memory_context_token_budget=max(
            0, _int("SOORIN_MEMORY_CONTEXT_TOKEN_BUDGET", 1400)
        ),
        long_term_memory_enabled=_bool("SOORIN_LONG_TERM_MEMORY_ENABLED", False),
        long_term_memory_backend=_choice(
            "SOORIN_LONG_TERM_MEMORY_BACKEND", "sqlite", {"sqlite"}
        ),
        memory_vector_index_enabled=_bool("SOORIN_MEMORY_VECTOR_INDEX_ENABLED", False),
        memory_qdrant_collection=os.getenv(
            "SOORIN_MEMORY_QDRANT_COLLECTION", "soorin_copilot_memory_v1"
        ).strip(),
        memory_retrieval_candidate_k=max(
            1, min(100, _int("SOORIN_MEMORY_RETRIEVAL_CANDIDATE_K", 20))
        ),
        memory_retrieval_top_k=max(
            1, min(20, _int("SOORIN_MEMORY_RETRIEVAL_TOP_K", 5))
        ),
        memory_min_score=min(1.0, max(0.0, _float("SOORIN_MEMORY_MIN_SCORE", 0.35))),
        memory_rerank_enabled=_bool("SOORIN_MEMORY_RERANK_ENABLED", False),
        memory_rerank_model=os.getenv("SOORIN_MEMORY_RERANK_MODEL", "").strip(),
        memory_rerank_timeout_seconds=max(
            0.1, _float("SOORIN_MEMORY_RERANK_TIMEOUT_SECONDS", 10.0)
        ),
        memory_context_long_term_token_budget=max(
            0, _int("SOORIN_MEMORY_CONTEXT_LONG_TERM_TOKEN_BUDGET", 500)
        ),
        local_product_simulation_enabled=_bool(
            "SOORIN_LOCAL_PRODUCT_SIMULATION_ENABLED",
            False,
        ),
        streamlit_auth_backend=_choice(
            "SOORIN_STREAMLIT_AUTH_BACKEND",
            "none",
            {"none", "local_simulation", "oidc"},
        ),
        local_test_user_creation_enabled=_bool(
            "SOORIN_LOCAL_TEST_USER_CREATION_ENABLED",
            False,
        ),
        thread_state_backend=_choice(
            "SOORIN_THREAD_STATE_BACKEND",
            "memory",
            {"memory", "sqlite"},
        ),
        local_sqlite_path=os.getenv(
            "SOORIN_LOCAL_SQLITE_PATH",
            "data/runtime/copilot-local.sqlite3",
        ).strip(),
        langgraph_checkpoint_backend=_choice(
            "SOORIN_LANGGRAPH_CHECKPOINT_BACKEND",
            "none",
            {"none", "sqlite"},
        ),
        system_prompt_path=os.getenv("SOORIN_SYSTEM_PROMPT_PATH", "app/prompts/system_prompt.md").strip(),
        product_api_base_url=os.getenv("SOORIN_PRODUCT_API_BASE_URL", "").strip().rstrip("/"),
        product_topology_path=os.getenv("SOORIN_PRODUCT_TOPOLOGY_PATH", "/zeek/connections/unique-ip-pairs").strip(),
        product_asset_detection_path=os.getenv("SOORIN_PRODUCT_ASSET_DETECTION_PATH", "/asset-detection/test/{ip}").strip(),
        product_asset_profile_path=os.getenv("SOORIN_PRODUCT_ASSET_PROFILE_PATH", "/profile/{ip}").strip(),
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
        detection_cache_ttl_seconds=max(600, _int("SOORIN_DETECTION_CACHE_TTL_SECONDS", 600)),
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
        rag_enabled=_bool("SOORIN_RAG_ENABLED", False),
        rag_source_root=os.getenv("SOORIN_RAG_SOURCE_ROOT", "").strip(),
        rag_backend=os.getenv("SOORIN_RAG_BACKEND", "qdrant").strip().lower(),
        rag_collection=os.getenv("SOORIN_RAG_COLLECTION", DEFAULT_RAG_COLLECTION).strip(),
        rag_top_k=max(1, _int("SOORIN_RAG_TOP_K", 5)),
        rag_score_threshold=min(1.0, max(0.0, _float("SOORIN_RAG_SCORE_THRESHOLD", 0.35))),
        rag_qdrant_mode=os.getenv("SOORIN_RAG_QDRANT_MODE", "server").strip().lower(),
        rag_qdrant_url=os.getenv("SOORIN_RAG_QDRANT_URL", "http://127.0.0.1:6333").strip().rstrip("/"),
        rag_qdrant_path=os.getenv("SOORIN_RAG_QDRANT_PATH", "").strip(),
        rag_qdrant_api_key=os.getenv("SOORIN_RAG_QDRANT_API_KEY", "").strip(),
        rag_qdrant_timeout_seconds=max(0.1, _float("SOORIN_RAG_QDRANT_TIMEOUT_SECONDS", 10.0)),
        rag_embedding_model=os.getenv(
            "SOORIN_RAG_EMBEDDING_MODEL",
            DEFAULT_RAG_EMBEDDING_MODEL,
        ).strip(),
        rag_embedding_dimension=max(1, _int("SOORIN_RAG_EMBEDDING_DIMENSION", DEFAULT_RAG_EMBEDDING_DIMENSION)),
        rag_embedding_local_files_only=_bool("SOORIN_RAG_EMBEDDING_LOCAL_FILES_ONLY", True),
        rag_embedding_cache_dir=os.getenv("SOORIN_RAG_EMBEDDING_CACHE_DIR", "").strip(),
        rag_embedding_revision=os.getenv(
            "SOORIN_RAG_EMBEDDING_REVISION",
            "a5beb1e3e68b9ab74eb54cfd186867f64f240e1a",
        ).strip(),
        rag_distance=os.getenv("SOORIN_RAG_DISTANCE", "cosine").strip().lower(),
        rag_max_context_tokens=max(256, _int("SOORIN_RAG_MAX_CONTEXT_TOKENS", 3000)),
        rag_chunk_size_chars=max(200, _int("SOORIN_RAG_CHUNK_SIZE_CHARS", 1200)),
        rag_chunk_overlap_chars=max(0, _int("SOORIN_RAG_CHUNK_OVERLAP_CHARS", 150)),
        rag_upsert_batch_size=max(1, _int("SOORIN_RAG_UPSERT_BATCH_SIZE", 64)),
        intent_router_enabled=_bool("SOORIN_INTENT_ROUTER_ENABLED", True),
        intent_router_system_prompt_path=os.getenv(
            "SOORIN_INTENT_ROUTER_SYSTEM_PROMPT_PATH",
            "app/prompts/intent_router_system_prompt.md",
        ).strip(),
        intent_router_min_confidence=_float("SOORIN_INTENT_ROUTER_MIN_CONFIDENCE", 0.65),
        intent_router_retry_enabled=_bool("SOORIN_INTENT_ROUTER_RETRY_ENABLED", True),
        graph_auto_refresh_enabled=_bool("SOORIN_GRAPH_AUTO_REFRESH_ENABLED", True),
        graph_refresh_interval_seconds=max(600, _int("SOORIN_GRAPH_REFRESH_INTERVAL_SECONDS", 900)),
        graph_refresh_on_startup=_bool("SOORIN_GRAPH_REFRESH_ON_STARTUP", True),
        graph_refresh_startup_delay_seconds=_int("SOORIN_GRAPH_REFRESH_STARTUP_DELAY_SECONDS", 5),
        graph_refresh_jitter_seconds=_int("SOORIN_GRAPH_REFRESH_JITTER_SECONDS", 30),
        graph_refresh_max_consecutive_failures=_int("SOORIN_GRAPH_REFRESH_MAX_CONSECUTIVE_FAILURES", 5),
        graph_refresh_keep_raw_snapshots=_int("SOORIN_GRAPH_REFRESH_KEEP_RAW_SNAPSHOTS", 5),
        graph_refresh_keep_processed_snapshots=_int("SOORIN_GRAPH_REFRESH_KEEP_PROCESSED_SNAPSHOTS", 3),
        graph_snapshot_ttl_hours=max(0, _int("SOORIN_GRAPH_SNAPSHOT_TTL_HOURS", 72)),
        graph_optional_exports_enabled=_bool("SOORIN_GRAPH_OPTIONAL_EXPORTS_ENABLED", False),
        graph_refresh_lock_timeout_seconds=_int("SOORIN_GRAPH_REFRESH_LOCK_TIMEOUT_SECONDS", 60),
        graph_refresh_min_nodes=_int("SOORIN_GRAPH_REFRESH_MIN_NODES", 1),
        graph_refresh_min_edges=_int("SOORIN_GRAPH_REFRESH_MIN_EDGES", 0),
        graph_refresh_max_node_drop_ratio=_float("SOORIN_GRAPH_REFRESH_MAX_NODE_DROP_RATIO", 0.80),
        graph_refresh_max_edge_drop_ratio=_float("SOORIN_GRAPH_REFRESH_MAX_EDGE_DROP_RATIO", 0.90),
        copilot_human_trace_enabled=_bool(
            "SOORIN_HUMAN_TRACE_ENABLED",
            _bool("SOORIN_COPILOT_HUMAN_TRACE_ENABLED", False),
        ),
        copilot_human_trace_detail=_choice(
            "SOORIN_HUMAN_TRACE_DETAIL", "detailed", {"summary", "detailed"}
        ),
        evidence_snapshot_enabled=_bool("SOORIN_EVIDENCE_SNAPSHOT_ENABLED", False),
        evidence_snapshot_mode=_choice(
            "SOORIN_EVIDENCE_SNAPSHOT_MODE",
            "summary",
            {"none", "metadata", "summary", "redacted"},
        ),
        evidence_snapshot_root=os.getenv(
            "SOORIN_EVIDENCE_SNAPSHOT_ROOT", "data/runtime/evidence"
        ).strip(),
        evidence_snapshot_ttl_hours=max(1, _int("SOORIN_EVIDENCE_SNAPSHOT_TTL_HOURS", 48)),
        evidence_snapshot_max_requests=max(
            1, _int("SOORIN_EVIDENCE_SNAPSHOT_MAX_REQUESTS", 100)
        ),
        evidence_snapshot_max_total_bytes=max(
            1024, _int("SOORIN_EVIDENCE_SNAPSHOT_MAX_TOTAL_BYTES", 268435456)
        ),
        evidence_snapshot_max_bytes=max(
            1024, _int("SOORIN_EVIDENCE_SNAPSHOT_MAX_BYTES", 5242880)
        ),
        llm_usage_reporting_enabled=_bool("LLM_USAGE_REPORTING_ENABLED", False),
        llm_usage_reporting_url=os.getenv(
            "LLM_USAGE_REPORTING_URL", ""
        ).strip(),
    )
    settings.validate_product_paths()
    settings.validate_observability_configuration()
    settings.validate_local_persistence_configuration()
    settings.validate_long_term_memory_configuration()
    logger.info(
        "event=settings_loaded env_file_path=%s env_file_loaded=%s router_deployment=%s chat_deployment=%s product_base_url_configured=%s product_token_present=%s product_hwid_present=%s product_username_present=%s product_password_present=%s product_captcha_bypass_present=%s rag_enabled=%s rag_backend=%s rag_source_configured=%s rag_qdrant_mode=%s rag_qdrant_configured=%s local_product_simulation_enabled=%s streamlit_auth_backend=%s local_test_user_creation_enabled=%s thread_state_backend=%s langgraph_checkpoint_backend=%s",
        ENV_PATH,
        env_file_loaded,
        settings.intent_router_deployment,
        settings.chat_deployment,
        bool(settings.product_api_base_url),
        bool(settings.product_api_token),
        bool(settings.product_hwid),
        bool(settings.product_username),
        bool(settings.product_password),
        bool(settings.product_captcha_bypass),
        settings.rag_enabled,
        settings.rag_backend,
        bool(settings.rag_source_root),
        settings.rag_qdrant_mode,
        bool(settings.rag_qdrant_path if settings.rag_qdrant_mode == "local" else settings.rag_qdrant_url),
        settings.local_product_simulation_enabled,
        settings.streamlit_auth_backend,
        settings.local_test_user_creation_enabled,
        settings.thread_state_backend,
        settings.langgraph_checkpoint_backend,
    )
    return settings
