"""Provider-neutral LLM result helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional


LLMStreamEventType = Literal[
    "reasoning_delta",
    "answer_delta",
    "usage",
    "done",
    "error",
]


@dataclass(frozen=True)
class LLMStreamEvent:
    """Provider-neutral incremental event with no raw provider payload."""

    type: LLMStreamEventType
    text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    message: str = ""

    def to_public_dict(self) -> Dict[str, Any]:
        event: Dict[str, Any] = {"type": self.type}
        if self.text:
            event["text"] = self.text
        if self.data:
            event["data"] = dict(self.data)
        if self.message:
            event["message"] = self.message
        return event


@dataclass
class LLMProviderResult:
    text: str
    provider: str
    model: str
    finish_reason: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    status_code: Optional[int] = None
    endpoint: Optional[str] = None
    reasoning_present: bool = False
    reasoning_exposed: bool = False
    payload_format: str = "text"
    deployment: str = ""

    def to_public_dict(self) -> Dict[str, Any]:
        """Return safe metadata without raw responses or reasoning content."""
        return {
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
            "deployment": self.deployment,
            "finish_reason": self.finish_reason,
            "usage": dict(self.usage or {}),
            "latency_ms": self.latency_ms,
            "elapsed_ms": self.latency_ms,
            "status_code": self.status_code,
            "endpoint": self.endpoint,
            "reasoning_present": bool(self.reasoning_present),
            "reasoning_exposed": False,
            "payload_format": self.payload_format,
        }
