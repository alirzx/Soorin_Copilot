"""Conservative deployment-aware token estimates and output reservations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping

from src.core.context.models import approx_tokens


@dataclass(frozen=True)
class TokenEstimate:
    raw_tokens: int
    calibrated_tokens: int
    multiplier: float


class TokenEstimator:
    def __init__(self, *, deployment: str, model: str, multiplier: float = 1.35) -> None:
        self.deployment = deployment
        self.model = model
        self.multiplier = max(1.0, float(multiplier))

    def estimate_text(self, text: str) -> TokenEstimate:
        raw = approx_tokens(text)
        return TokenEstimate(raw, int(math.ceil(raw * self.multiplier)), self.multiplier)

    def estimate_messages(self, messages: Iterable[Mapping[str, str]]) -> TokenEstimate:
        raw = sum(approx_tokens(item.get("content", "")) + 4 for item in messages)
        return TokenEstimate(raw, int(math.ceil(raw * self.multiplier)), self.multiplier)

    @staticmethod
    def output_reservation(detail: str, deployment_max_tokens: int) -> int:
        requested = {"brief": 1536, "standard": 4096, "detailed": 6144, "deep": 6144, "report": 6144}.get(
            detail,
            4096,
        )
        return max(1, min(int(deployment_max_tokens), requested))
