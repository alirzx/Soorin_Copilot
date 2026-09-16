"""Backward-compatible Arvan binding for the shared OpenAI-compatible transport."""

from src.config.llm_deployments import LLMRoleConfig
from src.core.llm.providers import openai_compatible as _transport
from src.core.llm.providers.openai_compatible import OpenAICompatibleProvider

# Preserve the established offline-test/mock seam at
# ``src.core.llm.providers.arvan.requests.post``.
requests = _transport.requests


class ArvanProvider(OpenAICompatibleProvider):
    provider_name = "arvan"

    def __init__(self, deployment: LLMRoleConfig, *, enabled: bool = True) -> None:
        super().__init__(
            deployment,
            provider_name=self.provider_name,
            enabled=enabled,
            api_key_required=True,
        )


__all__ = ["ArvanProvider"]
