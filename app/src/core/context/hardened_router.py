"""Small semantic-router adapter for production structured-query hardening."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from src.core.context.intent import SemanticIntentRouter as LLMPrimarySemanticIntentRouter
from src.core.context.models import EntityResolution, IntentDecision
from src.core.context.structured_hardening import normalize_structured_query_for_language
from src.core.context.structured_routing import SemanticIntentRouter as StructuredSemanticIntentRouter
from src.core.memory.routing_state import SessionRoutingState


_STRUCTURED_HARDENING_PROMPT = """

## Structured selector hardening
For Asset-set search, `classification_summary` is also an allowed exact string selector.
Text selector matching is semantic case-insensitive equality: preserve the user's value meaning and do not use capitalization as a differentiator. This applies to asset_name, status, suggested_type, classification_summary, vendor, product, role, roles, tag, sub_tag, and enrichment_status. IP remains canonical IP equality; confidence/unknown scores use numeric ranges; last_detection_at uses timestamp ranges.
Distinguish strict wording from inclusive wording: "above/greater than/more than/over X" means strictly greater than X; "below/less than/under X" means strictly less than X; "at least X" and "at most X" are inclusive.
For "highest/top" or "lowest/bottom" requests, emit the appropriate sort field and direction. The runtime may retrieve an additional leading row to determine whether the top value is tied; do not treat that bounded tie-check as a failure.
Use normal cybersecurity, SOC, NOC, NDR, threat-intelligence, identity, networking, and infrastructure domain knowledge to interpret natural wording and abbreviations. The following are non-exhaustive hints, not an input-language allow-list: manufacturer/maker/made by -> vendor; suggested type/asset type/device type/kind of asset -> suggested_type; primary role/primary function/serves as/acts as -> role; includes role/also has role/carries role -> roles; classification confidence/classifier confidence/model certainty -> model_confidence; mapping certainty/role-mapping confidence -> mapping_confidence; unknown probability/uncertainty score -> unknown_score; product family -> product; asset label -> tag; sub-tag/secondary tag -> sub_tag; asset name/hostname/name -> asset_name; last detected/last classified/detection timestamp -> last_detection_at; classification description/classifier summary -> classification_summary.
Natural-language vocabulary is open, but the output schema, selector fields, operators, entity bindings, and execution semantics remain closed and allow-listed. Do not collapse role into roles or invent fields, operators, thresholds, dates, values, or Cypher.

When `semantic_catalog.available=true`, treat the supplied catalog values as the current canonical inventory vocabulary. For broad user concepts, abbreviations, or shortened names, prefer catalog-backed canonical values over echoing the user's wording literally. This is semantic normalization, not substring execution: the runtime will still execute exact validated values only.
- If one catalog value clearly specializes the user's broader concept, use that canonical value. Examples of the reasoning pattern include a broad platform/function word resolving to a current `... Server`, `... Controller`, or similar canonical value when that is the one supported inventory meaning.
- If the user's concept is intentionally broader than several current canonical values, include all materially matching values with a bounded typed OR/IN rather than arbitrarily choosing one. For example, a broad `Windows` request should cover all current Windows class/function variants supplied by the catalog, not silently select only server or only workstation/client.
- Common domain abbreviations may be interpreted semantically, but emitted selector values must come from the current catalog when the catalog contains the relevant class/function. For example, `DC` may map to the catalog's Domain Controller value rather than the literal string `DC`.
- Do not invent an `os` selector. The structured Asset-set schema has no OS field. Broad terms such as Linux or Windows are class/function concepts for structured search unless the user explicitly asks a focal Product Profile OS question.
- If several materially different catalog mappings remain plausible and the user's wording does not intentionally include all of them, choose `unclear` instead of guessing.

Natural score thresholds may be fractions or percentages: 0.90, 90, 90%, and 90 percent all mean 0.90. Values outside 0..1/0..100, negative values, and ambiguous bare decimals above one must fail closed.
""".strip()


class SemanticIntentRouter(StructuredSemanticIntentRouter):
    """LLM-primary Router with deterministic structured validation and fallback."""

    def __init__(self, settings: Any, llm_client: Any) -> None:
        super().__init__(settings, llm_client)
        self.system_prompt = f"{self.system_prompt}\n\n{_STRUCTURED_HARDENING_PROMPT}"

    def classify(
        self,
        message: str,
        entities: EntityResolution,
        routing_state: SessionRoutingState,
        *,
        ui_context: dict[str, Any] | None = None,
        recent_messages: list[dict[str, str]] | None = None,
        trace_id: str = "",
        request_id: str = "",
    ) -> IntentDecision:
        """Always call the semantic Router for enabled routing.

        The structured layer remains responsible for validating and normalizing
        the model output through ``self._decision_from_content``. If the Router
        transport or schema/repair path fails, the established workflow returns
        ``fallback_used=True`` and ``StructuredAwareFallbackRouter`` performs the
        bounded deterministic structured parse. In other words, deterministic
        parsing is a safety/fallback mechanism, never the primary semantic
        authority.
        """

        return LLMPrimarySemanticIntentRouter.classify(
            self,
            message,
            entities,
            routing_state,
            ui_context=ui_context,
            recent_messages=recent_messages,
            trace_id=trace_id,
            request_id=request_id,
        )

    def _decision_from_content(
        self,
        content: str,
        finish_reason: str | None,
        entities: EntityResolution,
        routing_context: dict[str, Any],
        routing_state: SessionRoutingState | None,
        ui_context: dict[str, Any] | None,
        request_id: str,
    ) -> IntentDecision:
        decision = super()._decision_from_content(
            content,
            finish_reason,
            entities,
            routing_context,
            routing_state,
            ui_context,
            request_id,
        )
        if decision.structured_query is None:
            return decision
        query = normalize_structured_query_for_language(
            decision.structured_query,
            str(routing_context.get("message") or ""),
        )
        if self.semantic_catalog_provider is not None:
            query = self.semantic_catalog_provider.canonicalize_query(
                query,
                request_id=request_id,
            )
        return replace(decision, structured_query=query)
