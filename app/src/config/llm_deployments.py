"""Typed role-based configuration for OpenAI-compatible LLM calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit


LLMRoleName = Literal["router", "planner", "investigator", "synthesizer"]
LLMPurpose = Literal[
    "intent_router", "intent_router_repair", "planner", "planner_repair", "investigator", "chat"
]
VALID_LLM_ROLES: tuple[LLMRoleName, ...] = ("router", "planner", "investigator", "synthesizer")
SUPPORTED_LLM_PROVIDER_TYPES = frozenset(
    {"arvan", "vllm", "ollama", "openai_compatible"}
)


def normalize_chat_endpoint(base_url: str, chat_path: str) -> str:
    """Build one idempotent chat-completions endpoint from a role base URL."""
    normalized_base = base_url.strip().rstrip("/")
    normalized_path = chat_path.strip() or "/chat/completions"
    if not normalized_base:
        return ""
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    if normalized_base.endswith(normalized_path):
        return normalized_base
    return f"{normalized_base}{normalized_path}"


@dataclass(frozen=True)
class LLMRequestConfig:
    """Resolved limits and optional sampling controls for one request purpose."""

    max_tokens: int
    read_timeout_seconds: int
    temperature: float | None
    top_p: float | None


@dataclass(frozen=True)
class LLMRoleConfig:
    """One independently configurable Copilot role on an OpenAI-compatible gateway."""

    name: LLMRoleName
    base_url: str
    chat_path: str
    model: str
    api_key: str
    auth_scheme: str
    connect_timeout_seconds: int
    role_max_tokens: int
    maximum_completion_tokens: int
    timeout_seconds: int
    retry_max_tokens: int
    temperature: float | None
    top_p: float | None
    supports_temperature: bool
    supports_top_p: bool
    provider_type: str
    request_options: tuple[tuple[str, Any], ...] = ()

    @property
    def endpoint(self) -> str:
        return normalize_chat_endpoint(self.base_url, self.chat_path)

    @property
    def safe_host(self) -> str:
        """Return only the hostname, never a credential-bearing URL path."""
        return urlsplit(self.base_url).hostname or ""

    def request_config(self, purpose: str) -> LLMRequestConfig:
        """Resolve role limits while preserving the router/planner repair behavior."""
        if purpose in {"intent_router_repair", "planner_repair"}:
            requested_max_tokens = self.retry_max_tokens
        elif purpose in {"intent_router", "planner", "investigator", "chat"}:
            requested_max_tokens = self.role_max_tokens
        else:
            raise ValueError(f"Unsupported LLM request purpose: {purpose}")

        return LLMRequestConfig(
            max_tokens=max(1, requested_max_tokens),
            read_timeout_seconds=max(1, self.timeout_seconds),
            temperature=self.temperature if self.supports_temperature else None,
            top_p=self.top_p if self.supports_top_p else None,
        )

    def request_options_dict(self) -> dict[str, Any]:
        """Return role options without allowing core payload fields to be replaced."""
        protected = {"model", "messages", "max_tokens", "temperature", "top_p"}
        return {key: value for key, value in self.request_options if key not in protected}
