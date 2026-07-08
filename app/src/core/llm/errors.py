"""LLM runtime error types."""


class LLMError(RuntimeError):
    """Base error for local LLM runtime failures."""

    def __init__(self, message: str, *, reason: str = "generation_failed", details: dict | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details or {}


class LLMDisabledError(LLMError):
    """Raised when an LLM endpoint is requested while the LLM layer is disabled."""


class LLMJSONError(LLMError):
    """Raised when a model response cannot be parsed as valid JSON."""
