"""Strict non-streaming Investigator client and decision parser."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.core.agent.contracts import (
    AgentCapabilityRequest,
    AgentClarifyDecision,
    AgentContinueDecision,
    AgentDecision,
    AgentFinishDecision,
    StopReason,
)
from src.core.llm.errors import LLMError


logger = logging.getLogger(__name__)
DEFAULT_INVESTIGATOR_PROMPT_PATH = "app/prompts/investigator_system_prompt.md"


class InvestigatorError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CapabilityRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ContinuePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["CONTINUE"]
    evidence_gap_id: str = Field(min_length=1, max_length=100)
    capability_requests: list[CapabilityRequestPayload] = Field(min_length=1, max_length=2)
    assessment_summary: str = Field(default="", max_length=400)


class FinishPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["FINISH"]
    stop_reason: StopReason
    limitation_summary: str = Field(default="", max_length=400)


class ClarifyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["CLARIFY"]
    clarification_code: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    clarification_summary: str = Field(min_length=1, max_length=240)


class Investigator:
    """Call the dedicated role once and return only a typed decision."""

    def __init__(self, llm_client: Any, *, system_prompt_path: str = DEFAULT_INVESTIGATOR_PROMPT_PATH) -> None:
        self.llm_client = llm_client
        self.system_prompt_path = system_prompt_path
        self.system_prompt = self._load_prompt(system_prompt_path)

    @staticmethod
    def _load_prompt(configured_path: str) -> str:
        path = Path(configured_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            prompt = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("Tracked Investigator prompt is missing.") from exc
        if not prompt:
            raise RuntimeError("Tracked Investigator prompt is empty.")
        logger.info("event=investigator_prompt_loaded path=%s chars=%s", configured_path, len(prompt))
        return prompt

    def decide(
        self,
        context_json: str,
        *,
        request_id: str,
        trace_id: str = "",
    ) -> AgentDecision:
        try:
            result = self.llm_client.chat(
                [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": context_json},
                ],
                request_id=request_id,
                purpose="investigator",
                transient_retries=0,
                trace_id=trace_id,
            )
        except LLMError as exc:
            raise InvestigatorError("investigator_technical_failure", "Investigator provider call failed safely.") from exc
        text = str(getattr(result, "text", "") or "").strip()
        if not text:
            code = (
                "investigator_length_exhausted"
                if str(getattr(result, "finish_reason", "") or "") == "length"
                else "investigator_empty_output"
            )
            raise InvestigatorError(code, "Investigator did not return a usable decision.")
        return self.parse(text)

    @staticmethod
    def parse(text: str) -> AgentDecision:
        try:
            payload = json.loads(text.strip())
        except (TypeError, json.JSONDecodeError) as exc:
            raise InvestigatorError("investigator_malformed_json", "Investigator output was not one JSON object.") from exc
        if not isinstance(payload, dict):
            raise InvestigatorError("investigator_malformed_json", "Investigator output was not one JSON object.")
        kind = payload.get("kind")
        try:
            if kind == "CONTINUE":
                parsed = ContinuePayload.model_validate(payload)
                return AgentContinueDecision(
                    kind="CONTINUE",
                    evidence_gap_id=parsed.evidence_gap_id,
                    capability_requests=tuple(
                        AgentCapabilityRequest(item.capability, dict(item.arguments))
                        for item in parsed.capability_requests
                    ),
                    assessment_summary=parsed.assessment_summary,
                )
            if kind == "FINISH":
                parsed = FinishPayload.model_validate(payload)
                return AgentFinishDecision(
                    kind="FINISH",
                    stop_reason=parsed.stop_reason,
                    limitation_summary=parsed.limitation_summary,
                )
            if kind == "CLARIFY":
                parsed = ClarifyPayload.model_validate(payload)
                return AgentClarifyDecision(
                    kind="CLARIFY",
                    clarification_code=parsed.clarification_code,
                    clarification_summary=parsed.clarification_summary,
                )
        except ValidationError as exc:
            raise InvestigatorError("investigator_schema_invalid", "Investigator decision failed schema validation.") from exc
        raise InvestigatorError("investigator_kind_invalid", "Investigator decision kind is not supported.")
