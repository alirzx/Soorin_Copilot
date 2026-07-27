"""Typed configuration for named LLM deployments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit


LLMDeploymentName = Literal["kimi", "glm", "gpt55"]
LLMPurpose = Literal["intent_router", "intent_router_repair", "planner", "planner_repair", "chat"]
VALID_LLM_DEPLOYMENTS: tuple[LLMDeploymentName, ...] = ("kimi", "glm", "gpt55")


def normalize_chat_endpoint(base_url: str, chat_path: str) -> str:
    """Build one idempotent chat-completions endpoint from a deployment base URL."""
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
class ArvanDeploymentConfig:
    """One named deployment served through an OpenAI-compatible gateway."""

    name: LLMDeploymentName
    base_url: str
    chat_path: str
    model: str
    api_key: str
    auth_scheme: str
    connect_timeout_seconds: int
    maximum_completion_tokens: int
    router_read_timeout_seconds: int
    router_max_tokens: int
    router_repair_max_tokens: int
    chat_read_timeout_seconds: int
    chat_max_tokens: int
    router_temperature: float | None
    router_top_p: float | None
    chat_temperature: float | None
    chat_top_p: float | None
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
        """Resolve purpose-specific limits while enforcing the deployment maximum."""
        if purpose in {"intent_router", "planner"}:
            requested_max_tokens = self.router_max_tokens
            timeout_seconds = self.router_read_timeout_seconds
            temperature = self.router_temperature
            top_p = self.router_top_p
        elif purpose in {"intent_router_repair", "planner_repair"}:
            requested_max_tokens = self.router_repair_max_tokens
            timeout_seconds = self.router_read_timeout_seconds
            temperature = self.router_temperature
            top_p = self.router_top_p
        elif purpose == "chat":
            requested_max_tokens = self.chat_max_tokens
            timeout_seconds = self.chat_read_timeout_seconds
            temperature = self.chat_temperature
            top_p = self.chat_top_p
        else:
            raise ValueError(f"Unsupported LLM request purpose: {purpose}")

        return LLMRequestConfig(
            max_tokens=min(max(1, requested_max_tokens), max(1, self.maximum_completion_tokens)),
            read_timeout_seconds=max(1, timeout_seconds),
            temperature=temperature if self.supports_temperature else None,
            top_p=top_p if self.supports_top_p else None,
        )

    def request_options_dict(self) -> dict[str, Any]:
        """Return deployment options without allowing core payload fields to be replaced."""
        protected = {"model", "messages", "max_tokens", "temperature", "top_p"}
        return {key: value for key, value in self.request_options if key not in protected}
