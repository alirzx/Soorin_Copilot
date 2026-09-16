"""Central provider policy for the shared OpenAI-compatible transport."""

from src.config.llm_deployments import (
    LLMRoleConfig,
    SUPPORTED_LLM_PROVIDER_TYPES,
)
from src.core.llm.providers.arvan import ArvanProvider
from src.core.llm.providers.base import LLMProvider
from src.core.llm.providers.openai_compatible import OpenAICompatibleProvider


_API_KEY_REQUIRED_PROVIDERS = frozenset({"arvan"})


def provider_api_key_required(provider_name: str) -> bool:
    return provider_name in _API_KEY_REQUIRED_PROVIDERS


def build_provider(
    deployment: LLMRoleConfig,
    *,
    enabled: bool,
) -> LLMProvider | None:
    provider_name = deployment.provider_type
    if provider_name not in SUPPORTED_LLM_PROVIDER_TYPES:
        return None
    if provider_name == "arvan":
        return ArvanProvider(deployment, enabled=enabled)
    return OpenAICompatibleProvider(
        deployment,
        provider_name=provider_name,
        enabled=enabled,
        api_key_required=provider_api_key_required(provider_name),
    )
