"""Regression coverage for the Phase 4 structured Router extension contract."""

from __future__ import annotations

from dataclasses import replace
import json

from src.config.settings import get_settings
from src.core.context.models import EntityResolution
from src.core.context.structured_routing import SemanticIntentRouter, validate_structured_router_payload
from src.core.llm.providers.base import LLMProviderResult
from src.core.memory.routing_state import SessionRoutingState


def _empty_entities() -> EntityResolution:
    return EntityResolution(status="none", entity_mode="none")


def _search_payload() -> dict[str, object]:
    return {
        "intent": "asset_search",
        "scope": "none",
        "direction": "none",
        "depth": 0,
        "requires_graph": True,
        "requires_detection": False,
        "requires_asset_profile": False,
        "requires_knowledge": False,
        "structured_query": {
            "mode": "search",
            "filters": {"role": "Domain Controller"},
            "sort": "model_confidence",
            "direction": "desc",
        },
        "structured_result_reference": None,
        "entity_binding": "none",
        "requires_multiple_entities": False,
        "is_followup": False,
        "classification_confidence": 0.98,
        "reason": "Find matching Assets by structured properties.",
    }


def _general_payload() -> dict[str, object]:
    return {
        "intent": "general_knowledge",
        "scope": "none",
        "direction": "none",
        "depth": 0,
        "requires_graph": False,
        "requires_detection": False,
        "requires_asset_profile": False,
        "requires_knowledge": True,
        "structured_query": None,
        "structured_result_reference": None,
        "entity_binding": "none",
        "requires_multiple_entities": False,
        "is_followup": False,
        "classification_confidence": 0.95,
        "reason": "General cybersecurity knowledge request.",
    }


class _RepairLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses

    def chat(self, *_args, **_kwargs) -> LLMProviderResult:
        return LLMProviderResult(
            text=self.responses.pop(0),
            provider="fake",
            model="fake",
            finish_reason="stop",
            usage={},
            status_code=200,
        )


def test_normal_structured_route_accepts_prompt_contract_null_reference() -> None:
    decision = validate_structured_router_payload(
        _search_payload(),
        _empty_entities(),
        min_confidence=0.5,
    )

    assert decision.intent == "asset_search"
    assert decision.structured_query is not None
    assert decision.structured_result_reference.kind == "none"


def test_normal_legacy_route_accepts_prompt_contract_null_reference() -> None:
    decision = validate_structured_router_payload(
        _general_payload(),
        _empty_entities(),
        min_confidence=0.5,
    )

    assert decision.intent == "general_knowledge"
    assert decision.structured_query is None
    assert decision.structured_result_reference.kind == "none"


def test_router_repair_accepts_null_structured_result_reference() -> None:
    settings = replace(
        get_settings(),
        intent_router_enabled=True,
        intent_router_retry_enabled=True,
        intent_router_min_confidence=0.5,
    )
    router = SemanticIntentRouter(
        settings,
        _RepairLLM(["not json", json.dumps(_search_payload())]),
    )

    decision = router.classify(
        "list domain controllers ordered by model confidence",
        _empty_entities(),
        SessionRoutingState(),
        request_id="router-null-repair",
    )

    assert decision.decision_source == "semantic_router_repair"
    assert decision.intent == "asset_search"
    assert decision.structured_result_reference.kind == "none"
