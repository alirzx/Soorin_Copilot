"""Provider-neutral LLM client."""

from __future__ import annotations

from src.config.settings import Settings
from src.core.llm.errors import LLMDisabledError, LLMError
from src.core.llm.providers.arvan import ArvanProvider
from src.core.llm.providers.base import LLMProviderResult


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if settings.llm_provider == "arvan":
            self.provider = ArvanProvider(settings)
        else:
            self.provider = None

    def chat(self, messages: list[dict[str, str]]) -> LLMProviderResult:
        if not self.settings.llm_enabled:
            raise LLMDisabledError("LLM is disabled.", reason="llm_disabled")
        if self.provider is None:
            raise LLMError("Unsupported LLM provider.", reason="provider_not_supported")
        return self.provider.chat(messages)

    def health(self) -> dict[str, object]:
        if self.provider is None:
            return {
                "enabled": self.settings.llm_enabled,
                "ready": False,
                "provider": self.settings.llm_provider,
                "model": self.settings.arvan_model,
                "reason": "provider_not_supported",
            }
        return self.provider.health()
