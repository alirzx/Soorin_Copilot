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


@dataclass(frozen=True)
class TokenWindowBudget:
    context_window: int
    calibrated_input: int
    output_reservation: int
    configured_safety_margin: int
    remaining_before_safety: int
    remaining_usable_tokens: int
    fits: bool


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
    def output_reservation(
        detail: str,
        deployment_max_tokens: int,
        *,
        brief_output_tokens: int,
        standard_output_tokens: int,
        deep_output_tokens: int,
    ) -> int:
        requested = {
            "brief": brief_output_tokens,
            "standard": standard_output_tokens,
            "detailed": deep_output_tokens,
            "deep": deep_output_tokens,
            "report": deep_output_tokens,
        }.get(detail, standard_output_tokens)
        return max(1, min(int(deployment_max_tokens), requested))

    @staticmethod
    def window_budget(
        calibrated_input: int,
        output_reservation: int,
        configured_safety_margin: int,
        context_window: int,
    ) -> TokenWindowBudget:
        before_safety = int(context_window) - int(calibrated_input) - int(output_reservation)
        usable = before_safety - int(configured_safety_margin)
        return TokenWindowBudget(
            context_window=int(context_window),
            calibrated_input=int(calibrated_input),
            output_reservation=int(output_reservation),
            configured_safety_margin=int(configured_safety_margin),
            remaining_before_safety=before_safety,
            remaining_usable_tokens=usable,
            fits=usable >= 0,
        )
