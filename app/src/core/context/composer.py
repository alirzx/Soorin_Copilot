"""Compose bounded, deterministic model-facing provider context."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

from src.config.settings import Settings, get_settings
from src.core.agent.contracts import (
    StructuredAssetAggregateEvidence,
    StructuredAssetSearchEvidence,
)
from src.core.context.models import CopilotContextPackage, approx_tokens
from src.core.context.compaction import (
    CurrentEvidenceProjection,
    HistoricalBaselineProjection,
    build_delta_context,
    deduplicate_payloads,
)
from src.core.context.product_views import build_product_view, payload_inventory
from src.core.observability.metrics import get_metrics


logger = logging.getLogger(__name__)

PROVIDER_SEMANTICS = {
    "asset_profile": {
        "description": "Product inventory, identity, risk, service, alert, authentication, and current asset-state evidence.",
        "limitation": "Nested detection-related fields may share lineage with Asset Detection; repeated fields are not automatically independent corroboration.",
    },
    "asset_detection": {
        "description": "Product-generated classifier output, rules, signals, metrics, confidence, conflicts, and supporting evidence.",
        "limitation": "Classification evidence is not automatically authoritative inventory truth.",
    },
    "graph": {
        "description": "Observed topology and structured organizational Asset projection.",
        "limitation": "The projection is not live Product profile or detection truth. Topology does not prove protocol purpose, trust, dependency, authentication, compromise, routing, or attack paths; coverage may be partial.",
    },
    "knowledge": {
        "description": "Approved SOC documentation, runbooks, protocol knowledge, hardening guidance, and investigation procedures.",
        "limitation": "Documentation is not authoritative for current assets, graph relationships, detections, alerts, risk values, or peer lists.",
    },
}

VALUE_SEMANTICS = (
    "Missing, null, false, zero, empty string, empty array, empty object, provider unavailable, and not observed are distinct. "
    "A current zero does not prove the condition never existed historically."
)

GRAPH_GROUNDING_RULES = [
    "Disclose partial graph evidence.",
    "Distinguish aggregate totals from returned peer identities; never invent missing peers.",
    "Do not say all connections, every peer, or complete neighborhood unless complete_for_user_request is true.",
    "When truncated, use returned subset, retrieved peers, or summary evidence.",
    "Zero returned peers does not imply zero total peers.",
    "Topology does not prove protocol purpose, trust, dependency, authentication, compromise, routing capability, or attack paths.",
]

COMPARISON_CONTEXT_TOP_K = 12
NODE_SUMMARY_CONTEXT_MAX_TOKENS = 700
RELATIONSHIP_CONTEXT_MAX_TOKENS = 700
PATH_CONTEXT_MAX_TOKENS = 1200
ONE_HOP_CONTEXT_MAX_TOKENS = 1800
TWO_HOP_CONTEXT_MAX_TOKENS = 2500
COMPARISON_CONTEXT_MAX_TOKENS = 2200
FULL_NEIGHBORS_CONTEXT_MAX_TOKENS = 3000
STRUCTURED_ASSET_SEARCH_CONTEXT_MAX_TOKENS = 1800
STRUCTURED_ASSET_AGGREGATE_CONTEXT_MAX_TOKENS = 700
STRUCTURED_ASSET_SEARCH_MAX_CONTEXT_ROWS = 20
PROFILE_CONTEXT_MAX_TOKENS = 3200
DETECTION_CONTEXT_MAX_TOKENS = 2400

MINIMUM_PRODUCT_VIEWS = {
    "asset_profile": ("overview", "identity", "security", "network"),
    "detection": ("overview", "evidence"),
}


class ContextComposer:
    """Turn typed provider results into bounded model evidence without lossy product transforms."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.last_parts: dict[str, str] = {
            "status": "",
            "asset_profile": "",
            "detection": "",
            "graph": "",
            "knowledge": "",
            "delta": "",
            "fusion": "",
        }
        self.last_inclusion: dict[str, tuple[bool, str | None]] = {}
        self.last_representation: dict[str, str] = {}
        self.required_context_missing = False
        self.last_budget: dict[str, int] = {}
        self.last_knowledge_included_count = 0
        self.last_recomposition_attempted = False
        self.required_context_missing_reason: str | None = None
        self._product_projection_stats: dict[str, tuple[int, int]] = {}
        self.last_delta_contexts: tuple[dict[str, Any], ...] = ()
        self.last_delta_skip_reason: str | None = None
        self.last_baseline_status = "absent"
        self.last_baseline_present = False
        self.last_baseline_compatible = False

    def compose(
        self,
        package: CopilotContextPackage,
        *,
        request_id: str = "",
        base_input_tokens: int = 0,
        reserved_output_tokens: int | None = None,
        current_projections: tuple[CurrentEvidenceProjection, ...] = (),
        historical_baselines: tuple[HistoricalBaselineProjection, ...] = (),
    ) -> str:
        return self._compose_product_first(
            package,
            request_id=request_id,
            base_input_tokens=base_input_tokens,
            reserved_output_tokens=reserved_output_tokens,
            current_projections=current_projections,
            historical_baselines=historical_baselines,
        )

    def _compose_product_first(
        self,
        package: CopilotContextPackage,
        *,
        request_id: str,
        base_input_tokens: int,
        reserved_output_tokens: int | None,
        current_projections: tuple[CurrentEvidenceProjection, ...],
        historical_baselines: tuple[HistoricalBaselineProjection, ...],
    ) -> str:
        """Allocate complete Product evidence before bounded Graph and Knowledge context."""
        self._product_projection_stats = {}
        profile_sections = self._compose_json_sections(
            package.asset_profiles,
            "ASSET_PROFILE_CONTEXT_JSON",
        )
        detection_sections = self._compose_json_sections(
            package.detections,
            "ASSET_DETECTION_CONTEXT_JSON",
        )
        profile_sections, detection_sections, dedup_support, collapsed_count = self._deduplicate_product_sections(
            profile_sections,
            detection_sections,
        )
        profile_minimum_tokens = self._minimum_product_tokens(
            profile_sections,
            package.asset_profiles,
        )
        detection_minimum_tokens = self._minimum_product_tokens(
            detection_sections,
            package.detections,
        )
        output_reserve = (
            self.settings.llm_reserved_output_tokens
            if reserved_output_tokens is None
            else max(1, int(reserved_output_tokens))
        )
        calibrated_capacity = max(
            0,
            self.settings.llm_context_window_tokens
            - output_reserve
            - self.settings.llm_context_safety_margin_tokens
            - base_input_tokens,
        )
        max_dynamic_tokens = int(
            calibrated_capacity / max(1.0, self.settings.llm_token_estimate_multiplier)
        )
        self.required_context_missing = False
        self.required_context_missing_reason = None
        self.last_recomposition_attempted = False
        self.last_inclusion = {"knowledge": (False, "not_included")}
        self.last_representation = {"knowledge": "excluded"}
        self.last_knowledge_included_count = 0
        self.last_budget = {
            "base_input_tokens": base_input_tokens,
            "max_dynamic_tokens": max_dynamic_tokens,
            "reserved_output_tokens": output_reserve,
            "calibrated_dynamic_capacity": calibrated_capacity,
        }

        graph_reserve = min(
            max_dynamic_tokens // 2,
            sum(self._graph_context_cap(item.context) for item in package.graph_results),
        )
        minimum_product_tokens = profile_minimum_tokens + detection_minimum_tokens
        product_global_cap = min(
            max_dynamic_tokens,
            max(max(0, max_dynamic_tokens - graph_reserve), minimum_product_tokens),
        )
        profile_cap = min(
            PROFILE_CONTEXT_MAX_TOKENS,
            max(profile_minimum_tokens, product_global_cap - detection_minimum_tokens),
        )
        profile_text, profile_used = self._fit_product_sections(
            profile_sections,
            profile_cap,
            package.asset_profiles,
        )
        detection_text, detection_used = self._fit_product_sections(
            detection_sections,
            min(DETECTION_CONTEXT_MAX_TOKENS, max(0, product_global_cap - profile_used)),
            package.detections,
        )
        if dedup_support and approx_tokens(dedup_support) <= max(0, product_global_cap - profile_used - detection_used):
            detection_text = "\n\n".join(part for part in (detection_text, dedup_support) if part)
            detection_used += approx_tokens(dedup_support)
        for index, graph_result in enumerate(package.graph_results):
            identity = str(graph_result.context.get("context_identity") or f"graph:{index}")
            self.last_inclusion[identity] = (False, "not_included")
            self.last_representation[identity] = "excluded"

        manifest = self._compose_provider_manifest(package, graph_included=False)
        product_text = "\n\n".join(part for part in (profile_text, detection_text) if part)
        product_required_text = "\n\n".join(
            part for part in (manifest, product_text) if part
        )
        product_required_tokens = approx_tokens(product_required_text)
        self.last_budget["required_product_tokens"] = approx_tokens(product_text)
        self.last_budget["manifest_tokens"] = approx_tokens(manifest)
        if product_required_tokens > max_dynamic_tokens:
            self.required_context_missing_reason = "required_product_projections_exceed_context"

        delta_text = self._compose_delta_context(
            current_projections,
            historical_baselines,
            request_id=request_id,
            token_budget=min(1200, max(0, max_dynamic_tokens - product_required_tokens)),
        )
        used_tokens = product_required_tokens + approx_tokens(delta_text)
        included_graphs: list[tuple[str, CopilotContextPackage, str]] = []
        for index, graph_result in enumerate(package.graph_results):
            identity = str(graph_result.context.get("context_identity") or f"graph:{index}")
            single_package = replace(package, graph=graph_result, graphs=[])
            remaining = max(0, max_dynamic_tokens - used_tokens)
            graph_budget = min(self._graph_context_cap(graph_result.context), remaining)
            graph_text = self._compose_graph(
                single_package,
                request_id=request_id,
                token_budget=graph_budget,
            ) if graph_budget > 0 else ""
            included = bool(graph_text) and approx_tokens(graph_text) <= graph_budget
            reason = None if included else "global_context_limit_after_scope_compaction"
            self.last_inclusion[identity] = (included, reason)
            self.last_representation[identity] = (
                str(graph_result.context.get("context_mode") or "scope_summary")
                if included
                else "excluded"
            )
            if included:
                included_graphs.append((identity, single_package, graph_text))
                used_tokens += approx_tokens(graph_text)
            else:
                self._mark_graph_excluded(single_package, reason)
                self.required_context_missing = True
                self.required_context_missing_reason = "required_graph_context_excluded"

        if len(package.graph_results) == 1:
            identity = str(package.graph_results[0].context.get("context_identity") or "graph:0")
            self.last_inclusion["graph"] = self.last_inclusion[identity]
            self.last_representation["graph"] = self.last_representation[identity]

        graph_text = "\n\n".join(item[2] for item in included_graphs)
        remaining = max(0, max_dynamic_tokens - used_tokens)
        knowledge_text = self._compose_knowledge(
            package,
            token_budget=min(self.settings.rag_max_context_tokens, remaining),
        )
        if knowledge_text and approx_tokens(knowledge_text) <= remaining:
            self.last_inclusion["knowledge"] = (True, None)
            self.last_representation["knowledge"] = (
                "complete"
                if package.knowledge
                and self.last_knowledge_included_count == package.knowledge.included_count
                else "bounded"
            )
        else:
            knowledge_text = ""
            self.last_knowledge_included_count = 0
            self.last_inclusion["knowledge"] = (False, "global_context_limit")
            self.last_representation["knowledge"] = "excluded"

        def assemble() -> tuple[str, str]:
            current_manifest = self._compose_provider_manifest(
                package,
                graph_included=bool(included_graphs),
            )
            current = "\n\n".join(
                part
                for part in (
                    current_manifest,
                    profile_text,
                    detection_text,
                    delta_text,
                    "\n\n".join(item[2] for item in included_graphs),
                    knowledge_text,
                )
                if part
            )
            return current_manifest, current

        manifest, text = assemble()
        if approx_tokens(text) > max_dynamic_tokens and knowledge_text:
            knowledge_text = ""
            self.last_knowledge_included_count = 0
            self.last_inclusion["knowledge"] = (False, "global_context_limit_after_manifest")
            self.last_representation["knowledge"] = "excluded"
            self.last_recomposition_attempted = True
            manifest, text = assemble()
        while approx_tokens(text) > max_dynamic_tokens and included_graphs:
            identity, single_package, _ = included_graphs.pop()
            self.last_inclusion[identity] = (False, "global_context_limit_after_manifest")
            self.last_representation[identity] = "excluded"
            self._mark_graph_excluded(single_package, "global_context_limit_after_manifest")
            self.required_context_missing = True
            self.required_context_missing_reason = "required_graph_context_excluded"
            self.last_recomposition_attempted = True
            manifest, text = assemble()

        graph_text = "\n\n".join(item[2] for item in included_graphs)
        if len(package.graph_results) == 1:
            identity = str(package.graph_results[0].context.get("context_identity") or "graph:0")
            self.last_inclusion["graph"] = self.last_inclusion[identity]
            self.last_representation["graph"] = self.last_representation[identity]
        self._recompute_required_context(package, profile_sections, detection_sections)
        self.last_parts = {
            "status": manifest,
            "asset_profile": profile_text,
            "detection": detection_text,
            "graph": graph_text,
            "knowledge": knowledge_text,
            "delta": delta_text,
            "fusion": delta_text,
        }
        self.last_budget["total_dynamic_tokens"] = approx_tokens(text)
        logger.info(
            "event=context_composed_product_first request_id=%s profile_tokens=%s detection_tokens=%s graph_tokens=%s knowledge_tokens=%s total_dynamic_tokens=%s max_dynamic_tokens=%s required_context_missing=%s reason=%s",
            request_id,
            approx_tokens(profile_text),
            approx_tokens(detection_text),
            approx_tokens(graph_text),
            approx_tokens(knowledge_text),
            approx_tokens(text),
            max_dynamic_tokens,
            self.required_context_missing,
            self.required_context_missing_reason or "none",
        )
        logger.info(
            "event=evidence_deduplicated request_id=%s collapsed_count=%s",
            request_id,
            collapsed_count,
        )
        logger.info(
            "event=context_budget_allocated request_id=%s profile_tokens=%s detection_tokens=%s graph_reserved_tokens=%s knowledge_tokens=%s",
            request_id,
            approx_tokens(profile_text),
            approx_tokens(detection_text),
            graph_reserve,
            approx_tokens(knowledge_text),
        )
        logger.info(
            "event=context_compaction_completed request_id=%s raw_estimated_tokens=%s compacted_tokens=%s token_savings_estimate=%s",
            request_id,
            sum(getattr(item, "raw_json_approx_tokens", 0) for item in [*package.asset_profiles, *package.detections]),
            approx_tokens(product_text),
            max(0, sum(getattr(item, "raw_json_approx_tokens", 0) for item in [*package.asset_profiles, *package.detections]) - approx_tokens(product_text)),
        )
        metrics = get_metrics()
        metrics.observe_context("profile", approx_tokens(profile_text), approx_tokens(profile_text))
        metrics.observe_context("detection", approx_tokens(detection_text), approx_tokens(detection_text))
        metrics.observe_context("graph", approx_tokens(graph_text), approx_tokens(graph_text))
        metrics.observe_context("knowledge", approx_tokens(knowledge_text), approx_tokens(knowledge_text))
        metrics.observe_context(
            "product",
            sum(
                getattr(item, "raw_json_approx_tokens", 0)
                for item in [*package.asset_profiles, *package.detections]
            ),
            approx_tokens(product_text),
        )
        return text

    def _compose_delta_context(
        self,
        current: tuple[CurrentEvidenceProjection, ...],
        baselines: tuple[HistoricalBaselineProjection, ...],
        *,
        request_id: str,
        token_budget: int,
    ) -> str:
        """Build only exact compatible Product-view deltas and preserve provenance."""
        self.last_delta_contexts = ()
        self.last_delta_skip_reason = None
        self.last_baseline_present = bool(baselines)
        self.last_baseline_compatible = False
        self.last_baseline_status = "absent" if not baselines else "incompatible"
        reason = "current_projection_unavailable"
        payloads: list[dict[str, Any]] = []
        if current and not baselines:
            reason = "baseline_absent"
        for projection in current:
            candidates = [item for item in baselines if item.owner_id == projection.owner_id]
            if not candidates:
                reason = "wrong_owner" if baselines else reason
                continue
            status_eligible = [item for item in candidates if item.status == "active"]
            if not status_eligible:
                statuses = {item.status for item in candidates}
                reason = (
                    "candidate_only" if statuses == {"candidate"} else
                    "superseded" if "superseded" in statuses else
                    "invalidated" if "invalidated" in statuses else
                    "inactive"
                )
                continue
            candidates = status_eligible
            if any(item.unresolved_conflict for item in candidates):
                reason = "unresolved_conflict"
                continue
            type_eligible = [
                item
                for item in candidates
                if item.memory_type in {"validated_finding", "approved_asset_fact", "investigation_baseline"}
            ]
            if not type_eligible:
                reason = "wrong_memory_type"
                continue
            candidates = type_eligible
            if not any(item.evidence_classes for item in candidates):
                reason = "wrong_evidence_class"
                continue
            candidates = [
                item
                for item in candidates
                if item.accessible
                and item.authoritative
                and item.freshness not in {"expired", "inactive"}
                and item.complete
            ]
            if not candidates:
                reason = (
                    "stale"
                    if any(item.freshness in {"expired", "inactive"} for item in type_eligible)
                    else "incomplete_baseline"
                    if any(not item.complete for item in type_eligible)
                    else "inactive"
                )
                continue
            same_capability = [item for item in candidates if item.capability == projection.capability]
            if not same_capability:
                reason = "wrong_capability"
                continue
            projection_entities = set(projection.entity_ids or (projection.entity,))
            same_entity = [
                item for item in same_capability
                if set(item.entity_ids or (item.entity,)) == projection_entities
            ]
            if not same_entity:
                reason = "wrong_entity"
                continue
            same_view = [item for item in same_entity if item.view == projection.view]
            if not same_view:
                reason = "wrong_view"
                continue
            same_scope = [item for item in same_view if item.scope == projection.scope]
            if not same_scope:
                reason = "wrong_scope"
                continue
            same_direction = [
                item for item in same_scope if item.direction == projection.direction
            ]
            if not same_direction:
                reason = "wrong_direction"
                continue
            same_depth = [item for item in same_direction if item.depth == projection.depth]
            if not same_depth:
                reason = "wrong_depth"
                continue
            compatible = [
                item
                for item in same_depth
                if item.schema_version == projection.schema_version
            ]
            if not compatible:
                reason = "wrong_schema"
                continue
            baseline = max(compatible, key=lambda item: (item.observed_at, item.memory_id))
            delta = build_delta_context(
                projection.payload,
                baseline=baseline.payload,
                current_identity=projection.identity,
                baseline_identity=baseline.identity,
                schema_version=projection.schema_version,
                baseline_schema_version=baseline.schema_version,
                baseline_accessible=True,
                current_complete=projection.complete,
            )
            if not delta.created:
                reason = delta.reason
                continue
            payloads.append({
                "entity": projection.entity,
                "capability": projection.capability,
                "view": projection.view,
                "schema_version": projection.schema_version,
                "baseline": {
                    "memory_id": baseline.memory_id,
                    "observed_at": baseline.observed_at,
                    "provenance": baseline.provenance,
                },
                "current_retrieved_at": projection.retrieved_at,
                "delta": delta.payload,
            })
        if payloads:
            self.last_baseline_compatible = True
            self.last_baseline_status = (
                "available"
                if len(payloads) == len(current) and all(item.complete for item in current)
                else "partial"
            )
        elif reason == "stale":
            self.last_baseline_status = "stale"
        elif reason == "incomplete_baseline":
            self.last_baseline_status = "partial"
        elif baselines:
            self.last_baseline_status = "incompatible"
        while payloads:
            text = "[SOORIN_DELTA_CONTEXT_JSON]\n" + json.dumps(
                {"comparisons": payloads},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ) + "\n[/SOORIN_DELTA_CONTEXT_JSON]"
            if approx_tokens(text) <= max(0, token_budget):
                self.last_delta_contexts = tuple(payloads)
                logger.info(
                    "event=delta_context_used request_id=%s comparison_count=%s",
                    request_id,
                    len(payloads),
                )
                return text
            payloads.pop()
            reason = "delta_context_budget_exceeded"
        self.last_delta_skip_reason = reason
        logger.info("event=delta_context_skipped request_id=%s reason=%s", request_id, reason)
        return ""

    @staticmethod
    def graph_context_cap(context: dict[str, Any]) -> int:
        scope = str(context.get("requested_scope") or context.get("scope") or "node_summary")
        capability = str(context.get("source_capability") or "")
        if (
            context.get("relationship_mode") == "compare"
            or scope == "multi_entity_comparison"
            or capability == "graph.compare_assets"
        ):
            return COMPARISON_CONTEXT_MAX_TOKENS
        if capability == "graph.get_relationship" or context.get("relationship_mode") == "direct":
            return RELATIONSHIP_CONTEXT_MAX_TOKENS
        if capability == "graph.search_assets" or scope == "asset_search":
            return STRUCTURED_ASSET_SEARCH_CONTEXT_MAX_TOKENS
        if capability == "graph.aggregate_assets" or scope == "asset_aggregate":
            return STRUCTURED_ASSET_AGGREGATE_CONTEXT_MAX_TOKENS
        return {
            "node_summary": NODE_SUMMARY_CONTEXT_MAX_TOKENS,
            "path": PATH_CONTEXT_MAX_TOKENS,
            "one_hop": ONE_HOP_CONTEXT_MAX_TOKENS,
            "two_hop": TWO_HOP_CONTEXT_MAX_TOKENS,
            "full_neighbors": FULL_NEIGHBORS_CONTEXT_MAX_TOKENS,
        }.get(scope, ONE_HOP_CONTEXT_MAX_TOKENS)

    _graph_context_cap = graph_context_cap

    @staticmethod
    def _mark_graph_excluded(package: CopilotContextPackage, reason: str) -> None:
        for graph in package.graph_results:
            context = graph.context
            context.update(
                {
                    "included_node_count": 0,
                    "included_edge_count": 0,
                    "context_node_count": 0,
                    "context_edge_count": 0,
                    "inbound_context_included": 0,
                    "outbound_context_included": 0,
                    "bidirectional_context_included": 0,
                    "serialized_context_complete_for_retrieved_subset": False,
                    "serialized_context_truncated": True,
                    "serialized_context_truncation_reason": reason,
                    "context_truncated": True,
                    "context_truncation_reason": reason,
                    "complete_for_user_request": False,
                    "model_input_graph_included": False,
                    "model_input_graph_complete": False,
                }
            )

    def _compose_json_sections(self, results: list[Any], tag: str) -> list[tuple[str, str, str]]:
        sections: list[tuple[str, str, str]] = []
        seen_entities: set[str] = set()
        provider_name = "asset_profile" if "ASSET_PROFILE" in tag else "detection"
        for result in results:
            if result.status not in {"available", "not_found"} or not result.serialized_json:
                continue
            if result.ip in seen_entities:
                continue
            seen_entities.add(result.ip)
            serialized = result.serialized_json
            try:
                parsed = json.loads(serialized)
            except (TypeError, ValueError):
                parsed = None
            already_projected = isinstance(parsed, dict) and "views" in parsed and "projection_metadata" in parsed
            if not result.full_payload_included and not already_projected and result.raw_payload is not None:
                projected = build_product_view(
                    result.raw_payload,
                    provider=provider_name,
                    views=("overview",),
                    detail="brief",
                    max_context_tokens=1000,
                    purpose="composer_default_projection",
                )
                serialized = json.dumps(projected.payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                parsed = projected.payload
            source_scalars = payload_inventory(result.raw_payload).scalar_count if result.raw_payload is not None else 0
            projected_scalars = payload_inventory(parsed).scalar_count if parsed is not None else 0
            self._product_projection_stats[f"{provider_name}:{result.ip}"] = (source_scalars, projected_scalars)
            sections.append(
                (
                    provider_name,
                    result.ip,
                    "\n".join((f'[{tag} ip="{result.ip}"]', serialized, f'[/{tag}]')),
                )
            )
        return sections

    def _fit_product_sections(
        self,
        sections: list[tuple[str, str, str]],
        token_cap: int,
        provider_results: list[Any],
    ) -> tuple[str, int]:
        selected: list[str] = []
        used = 0
        by_ip = {item.ip: item for item in provider_results}
        for provider, ip, section in sections:
            tokens = approx_tokens(section)
            key = f"{provider}:{ip}"
            result = by_ip.get(ip)
            if used + tokens <= max(0, token_cap):
                selected.append(section)
                used += tokens
                self.last_inclusion[key] = (True, None)
                self.last_representation[key] = (
                    "full_minified" if result and result.full_payload_included else "projected"
                )
            else:
                minimum = self._minimum_product_section(
                    provider,
                    ip,
                    result,
                    record_stats=True,
                )
                minimum_tokens = approx_tokens(minimum) if minimum else 0
                if minimum and used + minimum_tokens <= max(0, token_cap):
                    selected.append(minimum)
                    used += minimum_tokens
                    self.last_inclusion[key] = (True, "bounded_minimum_projection")
                    self.last_representation[key] = "bounded_minimum"
                else:
                    self.last_inclusion[key] = (False, "evidence_class_budget_exceeded")
                    self.last_representation[key] = "excluded"
        return "\n\n".join(selected), used

    def _minimum_product_section(
        self,
        provider: str,
        ip: str,
        result: Any,
        *,
        record_stats: bool = False,
    ) -> str:
        if result is None or result.raw_payload is None:
            return ""
        try:
            projected = build_product_view(
                result.raw_payload,
                provider=provider,
                views=MINIMUM_PRODUCT_VIEWS[provider],
                detail="brief",
                max_context_tokens=(
                    PROFILE_CONTEXT_MAX_TOKENS
                    if provider == "asset_profile"
                    else DETECTION_CONTEXT_MAX_TOKENS
                ),
                purpose="composer_minimum_projection",
            )
        except (KeyError, TypeError, ValueError):
            return ""
        tag = (
            "ASSET_PROFILE_CONTEXT_JSON"
            if provider == "asset_profile"
            else "ASSET_DETECTION_CONTEXT_JSON"
        )
        serialized = json.dumps(
            projected.payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if record_stats:
            self._product_projection_stats[f"{provider}:{ip}"] = (
                projected.inventory.scalar_count,
                projected.usable_fact_count,
            )
        return "\n".join((f'[{tag} ip="{ip}"]', serialized, f'[/{tag}]'))

    def _minimum_product_tokens(
        self,
        sections: list[tuple[str, str, str]],
        provider_results: list[Any],
    ) -> int:
        by_ip = {item.ip: item for item in provider_results}
        total = 0
        for provider, ip, _ in sections:
            minimum = self._minimum_product_section(provider, ip, by_ip.get(ip))
            if minimum:
                total += approx_tokens(minimum)
        return total

    def _recompute_required_context(
        self,
        package: CopilotContextPackage,
        profile_sections: list[tuple[str, str, str]],
        detection_sections: list[tuple[str, str, str]],
    ) -> None:
        required_product_keys = {
            f"{provider}:{ip}"
            for provider, ip, _ in (*profile_sections, *detection_sections)
        }
        product_missing = any(
            not self.last_inclusion.get(key, (False, None))[0]
            for key in required_product_keys
        )
        graph_missing = any(
            not self.last_inclusion.get(
                str(result.context.get("context_identity") or f"graph:{index}"),
                (False, None),
            )[0]
            for index, result in enumerate(package.graph_results)
        )
        self.required_context_missing = product_missing or graph_missing
        self.required_context_missing_reason = (
            "required_product_projection_excluded"
            if product_missing
            else "required_graph_context_excluded"
            if graph_missing
            else None
        )

    @staticmethod
    def _deduplicate_product_sections(
        profiles: list[tuple[str, str, str]],
        detections: list[tuple[str, str, str]],
    ) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]], str, int]:
        sections = [*profiles, *detections]
        parsed: list[tuple[str, Any]] = []
        wrappers: list[tuple[str, str, str, str]] = []
        for provider, ip, section in sections:
            lines = section.splitlines()
            if len(lines) < 3:
                continue
            parsed.append((f"{provider}:{ip}", json.loads("\n".join(lines[1:-1]))))
            wrappers.append((provider, ip, lines[0], lines[-1]))
        deduped = deduplicate_payloads(parsed)
        rebuilt = [
            (provider, ip, "\n".join((opening, json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True), closing)))
            for (provider, ip, opening, closing), payload in zip(wrappers, deduped.payloads)
        ]
        profile_count = len(profiles)
        supported = [item for item in deduped.canonical_facts if len(item["support"]) > 1]
        support_text = ""
        if supported:
            support_text = "[SOORIN_DEDUPLICATED_FACT_SUPPORT]\n" + json.dumps(
                supported,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ) + "\n[/SOORIN_DEDUPLICATED_FACT_SUPPORT]"
        return rebuilt[:profile_count], rebuilt[profile_count:], support_text, deduped.collapsed_count

    @staticmethod
    def _combined_status(results: list[Any]) -> str:
        if not results:
            return "skipped"
        statuses = {item.status for item in results}
        return next(iter(statuses)) if len(statuses) == 1 else "partial"

    def _product_coverage(self, results: list[Any], provider_name: str) -> dict[str, Any]:
        entities: dict[str, Any] = {}
        for item in results:
            key = f"{provider_name}:{item.ip}"
            included = self.last_inclusion.get(key, (False, None))[0]
            representation = self.last_representation.get(key, "excluded")
            source_scalar_count, projected_scalar_count = self._product_projection_stats.get(key, (0, 0))
            entities[item.ip] = {
                "status": item.status,
                "payload_included": included,
                "payload_complete": bool(
                    included
                    and not item.context_truncated
                    and representation != "bounded_minimum"
                ),
                "model_representation_projected": representation in {"projected", "bounded_minimum"},
                "representation": representation,
                "stale": bool(item.stale),
                "source_payload_complete": bool(item.raw_payload_present and not item.context_truncated),
                "full_payload_fetched": bool(item.full_payload_fetched),
                "projection_usable": bool(item.serialized_json),
                "usable_fact_count": projected_scalar_count,
                "projection_truncated": representation == "bounded_minimum",
                "projection_omitted_count": max(0, source_scalar_count - projected_scalar_count),
            }
        return {
            "status": self._combined_status(results),
            "payload_included": bool(entities) and all(value["payload_included"] for value in entities.values()),
            "payload_complete": bool(entities) and all(value["payload_complete"] for value in entities.values()),
            "stale": any(value["stale"] for value in entities.values()),
            "entities": entities,
        }

    def _compose_provider_manifest(self, package: CopilotContextPackage, *, graph_included: bool) -> str:
        requested: list[str] = []
        coverage: dict[str, Any] = {}
        if package.asset_profiles:
            requested.append("asset_profile")
            coverage["asset_profile"] = self._product_coverage(package.asset_profiles, "asset_profile")
        if package.detections:
            requested.append("asset_detection")
            coverage["asset_detection"] = self._product_coverage(package.detections, "detection")
        if package.graph_results:
            requested.append("graph")
            graph_coverage: dict[str, Any] = {}
            for index, graph_result in enumerate(package.graph_results):
                context = graph_result.context or {}
                identity = str(context.get("context_identity") or f"graph:{index}")
                result_included = self.last_inclusion.get(identity, self.last_inclusion.get("graph", (False, None)))[0]
                graph_coverage[identity] = {
                    "status": graph_result.status,
                    "payload_included": result_included,
                    "requested_scope": context.get("requested_scope", context.get("scope", "none")),
                    "candidate_node_count": context.get("candidate_node_count", 0),
                    "returned_node_count": context.get("retrieved_node_count", context.get("returned_node_count", 0)),
                    "candidate_edge_count": context.get("candidate_edge_count", 0),
                    "returned_edge_count": context.get("retrieved_edge_count", context.get("returned_edge_count", 0)),
                    "included_node_count": context.get("included_node_count", context.get("context_node_count", 0)),
                    "included_edge_count": context.get("included_edge_count", context.get("context_edge_count", 0)),
                    "model_context_token_estimate": context.get("model_context_token_estimate", 0),
                    "model_context_token_cap": context.get("model_context_token_cap", 0),
                    "model_context_omitted_peer_count": context.get("model_context_omitted_peer_count", 0),
                    "inbound_context_included": context.get("inbound_context_included", 0),
                    "outbound_context_included": context.get("outbound_context_included", 0),
                    "bidirectional_context_included": context.get("bidirectional_context_included", 0),
                    "retrieval_complete": context.get("retrieval_complete", False),
                    "retrieval_truncated": context.get("retrieval_truncated", False),
                    "retrieval_truncation_reason": context.get("retrieval_truncation_reason"),
                    "serialized_context_complete_for_retrieved_subset": context.get("serialized_context_complete_for_retrieved_subset", False),
                    "serialized_context_truncated": context.get("serialized_context_truncated", False),
                    "serialized_context_truncation_reason": context.get("serialized_context_truncation_reason"),
                    "requested_scope_complete": context.get("requested_scope_complete", False),
                    "model_input_graph_included": result_included,
                    "model_input_graph_complete": bool(
                        result_included
                        and context.get("serialized_context_complete_for_retrieved_subset", False)
                    ),
                    "complete_for_user_request": bool(result_included and context.get("complete_for_user_request", False)),
                    "omission_reason": self.last_inclusion.get(identity, (False, "missing_context_inclusion_decision"))[1],
                }
            coverage["graph_results"] = graph_coverage
            if len(graph_coverage) == 1:
                coverage["graph"] = next(iter(graph_coverage.values()))
        if package.knowledge:
            requested.append("knowledge")
            knowledge_included = self.last_inclusion.get("knowledge", (False, None))[0]
            coverage["knowledge"] = {
                "status": package.knowledge.status,
                "backend": package.knowledge.backend,
                "retrieved_at": package.knowledge.retrieved_at,
                "freshness": package.knowledge.freshness,
                "total_candidates": package.knowledge.total_candidates,
                "retrieved_chunk_count": package.knowledge.included_count,
                "included_chunk_count": self.last_knowledge_included_count if knowledge_included else 0,
                "retrieval_truncated": package.knowledge.truncated,
                "model_input_knowledge_included": knowledge_included,
                "model_input_omission_reason": self.last_inclusion.get("knowledge", (False, None))[1]
                if not knowledge_included
                else None,
                "model_input_knowledge_complete": bool(
                    knowledge_included
                    and self.last_knowledge_included_count == package.knowledge.included_count
                ),
                "serialized_context_truncated": bool(
                    knowledge_included
                    and self.last_knowledge_included_count < package.knowledge.included_count
                ),
                "limitations": list(package.knowledge.limitations),
            }
        target_entities = [entity.value for entity in package.entities.entities]
        if not target_entities and package.graph_results:
            first_context = package.graph_results[0].context
            target_entities = [str(item) for item in first_context.get("target_ips", []) if item]
            if not target_entities and first_context.get("target_ip"):
                target_entities = [str(first_context["target_ip"])]
        manifest = {
            "evidence_authority": "Current provider payloads and coverage are authoritative for this turn; previous assistant claims are conversation only.",
            "provider_coverage": coverage,
            "provider_semantics": {name: PROVIDER_SEMANTICS[name] for name in requested},
            "requested_providers": requested,
            "target_entities": target_entities,
            "value_semantics": VALUE_SEMANTICS,
        }
        return "[SOORIN_PROVIDER_MANIFEST]\n" + json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n[/SOORIN_PROVIDER_MANIFEST]"

    def _compose_knowledge(self, package: CopilotContextPackage, *, token_budget: int) -> str:
        result = package.knowledge
        self.last_knowledge_included_count = 0
        if not result or result.status not in {"ok", "partial", "empty"}:
            return ""
        base = {
            "status": result.status,
            "query": result.query,
            "backend": result.backend,
            "retrieved_at": result.retrieved_at,
            "freshness": result.freshness,
            "total_candidates": result.total_candidates,
            "retrieved_chunk_count": result.included_count,
            "retrieval_truncated": result.truncated,
            "limitations": list(result.limitations),
            "grounding_rule": "Use this only as documentation evidence; current operational providers outrank it.",
        }
        selected: list[dict[str, Any]] = []
        text = ""
        for chunk in result.chunks:
            candidate = {
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "relative_path": chunk.relative_path,
                "section": chunk.section,
                "category": chunk.category,
                "title": chunk.title,
                "score": chunk.score,
                "indexed_at": chunk.indexed_at,
                "source_version": chunk.source_version,
                "text": chunk.text,
            }
            trial = [*selected, candidate]
            payload = {
                **base,
                "included_chunk_count": len(trial),
                "serialized_context_truncated": len(trial) < result.included_count,
                "chunks": trial,
            }
            candidate_text = "[SOORIN_KNOWLEDGE_CONTEXT_JSON]\n" + json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ) + "\n[/SOORIN_KNOWLEDGE_CONTEXT_JSON]"
            if approx_tokens(candidate_text) > max(0, token_budget):
                break
            selected = trial
            text = candidate_text
        if result.status == "empty" and not result.chunks:
            payload = {**base, "included_chunk_count": 0, "serialized_context_truncated": False, "chunks": []}
            text = "[SOORIN_KNOWLEDGE_CONTEXT_JSON]\n" + json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ) + "\n[/SOORIN_KNOWLEDGE_CONTEXT_JSON]"
            if approx_tokens(text) > max(0, token_budget):
                return ""
        self.last_knowledge_included_count = len(selected)
        return text

    def _compose_graph(
        self,
        package: CopilotContextPackage,
        *,
        request_id: str = "",
        token_budget: int | None = None,
    ) -> str:
        graph = package.graph
        if not graph or graph.status not in {"available", "not_found"}:
            return ""
        context = graph.context or {}
        scope = str(context.get("requested_scope") or context.get("scope") or "node_summary")
        source_capability = str(context.get("source_capability") or "")
        resolved_budget = min(
            self._graph_context_cap(context),
            token_budget if token_budget is not None else self._graph_context_cap(context),
        )
        if source_capability in {"graph.search_assets", "graph.aggregate_assets"}:
            return self._compose_structured_asset_set(
                graph,
                token_budget=resolved_budget,
                request_id=request_id,
            )
        if (
            context.get("relationship_mode") == "compare"
            or scope == "multi_entity_comparison"
            or source_capability == "graph.compare_assets"
        ):
            return self._compose_comparison_graph(
                package,
                token_budget=resolved_budget,
                request_id=request_id,
            )
        if source_capability == "graph.get_relationship" or context.get("relationship_mode") == "direct":
            return self._compose_relationship_graph(package, resolved_budget, request_id)
        if scope == "node_summary":
            return self._compose_node_summary_graph(package, resolved_budget, request_id)
        if scope == "path" or source_capability == "graph.find_path":
            return self._compose_path_graph(package, resolved_budget, request_id)
        if scope in {"one_hop", "two_hop", "full_neighbors"}:
            return self._compose_neighborhood_graph(package, resolved_budget, request_id)
        nodes, edges, reasons = self._select_context_records(context)
        serialized_truncated = bool(reasons)
        serialized_reason = reasons[0] if reasons else None
        target_ip = str(context.get("target_ip") or "")
        direction_counts = self._included_direction_counts(nodes, target_ip)
        context.update(
            {
                "included_node_count": len(nodes),
                "included_edge_count": len(edges),
                "context_node_count": len(nodes),
                "context_edge_count": len(edges),
                "inbound_context_included": direction_counts["inbound"],
                "outbound_context_included": direction_counts["outbound"],
                "bidirectional_context_included": direction_counts["bidirectional"],
                "serialized_context_complete_for_retrieved_subset": not serialized_truncated,
                "serialized_context_truncated": serialized_truncated,
                "serialized_context_truncation_reason": serialized_reason,
                "context_truncated": serialized_truncated,
                "context_truncation_reasons": reasons,
                "context_truncation_reason": serialized_reason,
                "context_mode": "aggregate_only" if context.get("scope") == "node_summary" else "enumerated",
                "aggregate_only_context": context.get("scope") == "node_summary",
                "complete_for_user_request": bool(context.get("requested_scope_complete", False) and not serialized_truncated),
            }
        )
        inbound_peers = sorted(str(node["id"]) for node in nodes if node.get("id") != target_ip and node.get("inbound"))
        outbound_peers = sorted(str(node["id"]) for node in nodes if node.get("id") != target_ip and node.get("outbound"))
        bidirectional_peers = sorted(set(inbound_peers).intersection(outbound_peers))
        coverage = {
            "retrieval_complete": context.get("retrieval_complete", False),
            "retrieval_truncated": context.get("retrieval_truncated", False),
            "retrieval_truncation_reason": context.get("retrieval_truncation_reason"),
            "serialized_context_complete_for_retrieved_subset": not serialized_truncated,
            "serialized_context_truncated": serialized_truncated,
            "serialized_context_truncation_reason": serialized_reason,
            "requested_scope_complete": context.get("requested_scope_complete", False),
            "complete_for_user_request": context["complete_for_user_request"],
        }
        payload: dict[str, Any] = {
            "status": graph.status,
            "target_ip": target_ip,
            "target_ips": context.get("target_ips", [target_ip] if target_ip else []),
            "requested_scope": context.get("requested_scope", context.get("scope", "none")),
            "direction": context.get("direction", "none"),
            "depth": context.get("depth", 0),
            "coverage": coverage,
            "counts": {
                "candidate_nodes": context.get("candidate_node_count", 0),
                "returned_nodes": context.get("retrieved_node_count", context.get("returned_node_count", 0)),
                "serialized_nodes": len(nodes),
                "candidate_edges": context.get("candidate_edge_count", 0),
                "returned_edges": context.get("retrieved_edge_count", context.get("returned_edge_count", 0)),
                "serialized_edges": len(edges),
            },
            "relationships": {
                "inbound": {"total": context.get("inbound_total", 0), "returned": context.get("inbound_retrieved", context.get("inbound_returned", 0)), "peers": inbound_peers},
                "outbound": {"total": context.get("outbound_total", 0), "returned": context.get("outbound_retrieved", context.get("outbound_returned", 0)), "peers": outbound_peers},
                "bidirectional": {"total": context.get("bidirectional_total", 0), "returned": context.get("bidirectional_retrieved", context.get("bidirectional_returned", 0)), "peers": bidirectional_peers},
            },
            "nodes": nodes,
            "edges": edges,
            "limitations": list(context.get("limitations") or graph.limitations or []),
            "grounding_rules": GRAPH_GROUNDING_RULES,
        }
        if context.get("relationship_mode") == "direct":
            payload["direct_relationship"] = {
                "source": context.get("source"),
                "target": context.get("target"),
                "source_present": context.get("source_present", False),
                "target_present": context.get("target_present", False),
                "source_to_target": context.get("forward_edge", False),
                "target_to_source": context.get("reverse_edge", False),
                "relationship_status": context.get("relationship_status", context.get("relationship", "unknown")),
            }
        if context.get("relationship_mode") == "compare":
            entity_a = dict(context.get("entity_a") or {})
            entity_b = dict(context.get("entity_b") or {})
            payload["entities"] = {
                str(entity_a.get("ip", "entity_a")): {key: value for key, value in entity_a.items() if key != "peers_retrieved"},
                str(entity_b.get("ip", "entity_b")): {key: value for key, value in entity_b.items() if key != "peers_retrieved"},
            }
            payload["comparison"] = {
                "shared_peer_total": context.get("shared_peer_total", 0),
                "shared_peers_returned": context.get("shared_peers_retrieved", []),
                "entity_a_unique_peer_total": context.get("entity_a_unique_peer_total", 0),
                "entity_b_unique_peer_total": context.get("entity_b_unique_peer_total", 0),
                "relationship": context.get("direct_relationship", {}),
                "degree": context.get("degree_comparison", {}),
                "subnets": context.get("subnet_comparison", {}),
            }
        if "path_exists" in context:
            payload["path"] = {
                "exists": context.get("path_exists"),
                "hop_count": context.get("hop_count"),
                "nodes": context.get("path_nodes", []),
            }
        text = "[SOORIN_GRAPH_CONTEXT_JSON]\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n[/SOORIN_GRAPH_CONTEXT_JSON]"
        if token_budget is not None and approx_tokens(text) > token_budget:
            return self._compose_compact_graph(
                package,
                token_budget=token_budget,
                request_id=request_id,
            )
        logger.info(
            "event=context_composer_graph_complete request_id=%s graph_status=%s requested_scope=%s retrieval_complete=%s serialized_context_complete_for_retrieved_subset=%s requested_scope_complete=%s complete_for_user_request=%s returned_nodes=%s serialized_nodes=%s returned_edges=%s serialized_edges=%s context_chars=%s",
            request_id,
            graph.status,
            payload["requested_scope"],
            coverage["retrieval_complete"],
            coverage["serialized_context_complete_for_retrieved_subset"],
            coverage["requested_scope_complete"],
            coverage["complete_for_user_request"],
            payload["counts"]["returned_nodes"],
            len(nodes),
            payload["counts"]["returned_edges"],
            len(edges),
            len(text),
        )
        return text

    def _compose_structured_asset_set(
        self,
        graph: Any,
        *,
        token_budget: int,
        request_id: str,
    ) -> str:
        """Serialize typed Asset-set evidence separately from topology payloads."""
        context = graph.context
        evidence = context.get("structured_asset_set")
        if not isinstance(
            evidence,
            (StructuredAssetSearchEvidence, StructuredAssetAggregateEvidence),
        ):
            return ""
        authority = (
            "Exact values are from the active Neo4j organizational projection; "
            "they are not live Product profile or detection truth."
        )
        base = {
            "schema_version": evidence.schema_version,
            "capability": evidence.capability,
            "mode": evidence.mode,
            "query_identity": evidence.query_identity,
            "query": {"filters": evidence.normalized_filters},
            "source": {
                "provenance": evidence.provenance,
                "active_graph_version": evidence.active_graph_version,
                "retrieved_at": evidence.retrieved_at,
                "authority": authority,
            },
            "limitations": list(evidence.limitations),
        }
        if isinstance(evidence, StructuredAssetAggregateEvidence):
            base["query"].update(
                operation=evidence.operation,
                group_by=evidence.group_by,
                group_by_fields=list(evidence.group_by_fields),
            )
            selected_groups: list[dict[str, Any]] = []
            payload = {
                **base,
                "result": {
                    "count": evidence.count,
                    "groups_retrieved": len(evidence.groups),
                    "groups_in_model_context": 0,
                    "groups_omitted_from_model_context": len(evidence.groups),
                    "retrieval_truncated": evidence.truncated,
                    "context_truncated": bool(evidence.groups),
                    "groups": [],
                },
            }
            text = self._structured_asset_text(payload)
            if approx_tokens(text) > max(0, token_budget):
                return ""
            for group in evidence.groups:
                candidate = [*selected_groups, self._bounded_structured_value(group)]
                payload = {
                    **base,
                    "result": {
                        "count": evidence.count,
                        "groups_retrieved": len(evidence.groups),
                        "groups_in_model_context": len(candidate),
                        "groups_omitted_from_model_context": len(evidence.groups) - len(candidate),
                        "retrieval_truncated": evidence.truncated,
                        "context_truncated": len(candidate) < len(evidence.groups),
                        "groups": candidate,
                    },
                }
                candidate_text = self._structured_asset_text(payload)
                if approx_tokens(candidate_text) > max(0, token_budget):
                    break
                selected_groups = candidate
                text = candidate_text
            self._record_structured_context(
                context,
                text,
                token_budget,
                included_count=len(selected_groups),
                retrieved_count=len(evidence.groups),
            )
            return text

        base["query"].update(sort=evidence.sort, direction=evidence.direction)
        selected_rows: list[dict[str, Any]] = []
        text = ""
        candidate_rows = evidence.rows[:STRUCTURED_ASSET_SEARCH_MAX_CONTEXT_ROWS]
        for row in candidate_rows:
            candidate = [
                *selected_rows,
                self._structured_asset_row(row, evidence),
            ]
            payload = {
                **base,
                "coverage": {
                    "matched_total": evidence.matched_total,
                    "rows_retrieved": evidence.returned_count,
                    "rows_in_model_context": len(candidate),
                    "rows_omitted_from_model_context": evidence.returned_count - len(candidate),
                    "retrieval_truncated": evidence.truncated,
                    "context_truncated": len(candidate) < evidence.returned_count,
                },
                "assets": candidate,
            }
            candidate_text = self._structured_asset_text(payload)
            if approx_tokens(candidate_text) > max(0, token_budget):
                break
            selected_rows = candidate
            text = candidate_text
        if not evidence.rows:
            payload = {
                **base,
                "coverage": {
                    "matched_total": evidence.matched_total,
                    "rows_retrieved": 0,
                    "rows_in_model_context": 0,
                    "rows_omitted_from_model_context": 0,
                    "retrieval_truncated": evidence.truncated,
                    "context_truncated": False,
                },
                "assets": [],
            }
            text = self._structured_asset_text(payload)
            if approx_tokens(text) > max(0, token_budget):
                return ""
        self._record_structured_context(
            context,
            text,
            token_budget,
            included_count=len(selected_rows),
            retrieved_count=evidence.returned_count,
        )
        logger.info(
            "event=structured_asset_context_composed request_id=%s mode=%s matched_total=%s retrieved_count=%s model_count=%s retrieval_truncated=%s context_truncated=%s context_tokens=%s",
            request_id,
            evidence.mode,
            evidence.matched_total,
            evidence.returned_count,
            len(selected_rows),
            evidence.truncated,
            len(selected_rows) < evidence.returned_count,
            approx_tokens(text),
        )
        return text

    @staticmethod
    def _structured_asset_text(payload: dict[str, Any]) -> str:
        return (
            "[SOORIN_STRUCTURED_ASSET_SET_CONTEXT_JSON]\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n[/SOORIN_STRUCTURED_ASSET_SET_CONTEXT_JSON]"
        )

    @classmethod
    def _structured_asset_row(
        cls,
        row: dict[str, Any],
        evidence: StructuredAssetSearchEvidence,
    ) -> dict[str, Any]:
        required = {"graph_key", "ip", "asset_name"}
        requested = set(evidence.normalized_filters)
        requested.add(evidence.sort)
        useful = {
            "status",
            "suggested_type",
            "role",
            "roles",
            "vendor",
            "product",
            "model_confidence",
            "mapping_confidence",
            "unknown_score",
            "enrichment_status",
            "last_detection_at",
        }
        selected = required | requested | useful
        return {
            key: cls._bounded_structured_value(value)
            for key, value in row.items()
            if key in selected and value is not None
        }

    @classmethod
    def _bounded_structured_value(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value[:160]
        if isinstance(value, (list, tuple)):
            return [cls._bounded_structured_value(item) for item in value[:5]]
        if isinstance(value, dict):
            return {
                str(key)[:80]: cls._bounded_structured_value(item)
                for key, item in list(value.items())[:12]
            }
        return value

    @staticmethod
    def _record_structured_context(
        context: dict[str, Any],
        text: str,
        token_budget: int,
        *,
        included_count: int,
        retrieved_count: int,
    ) -> None:
        context.update(
            {
                "included_node_count": included_count,
                "model_context_included_count": included_count,
                "model_context_omitted_count": max(0, retrieved_count - included_count),
                "serialized_context_complete_for_retrieved_subset": included_count == retrieved_count,
                "serialized_context_truncated": included_count < retrieved_count,
                "context_truncated": included_count < retrieved_count,
                "context_mode": "structured_asset_set",
                "model_context_token_estimate": approx_tokens(text),
                "model_context_token_cap": token_budget,
                "model_input_graph_included": bool(text),
            }
        )

    @staticmethod
    def _graph_coverage(
        context: dict[str, Any],
        *,
        serialization_truncated: bool,
        reason: str | None,
    ) -> dict[str, Any]:
        return {
            "retrieval_complete": context.get("retrieval_complete", False),
            "retrieval_truncated": context.get("retrieval_truncated", False),
            "retrieval_truncation_reason": context.get("retrieval_truncation_reason"),
            "requested_scope_complete": context.get("requested_scope_complete", False),
            "serialized_context_complete_for_retrieved_subset": not serialization_truncated,
            "serialized_context_truncated": serialization_truncated,
            "serialized_context_truncation_reason": reason,
            "complete_for_user_request": bool(
                context.get("requested_scope_complete", False) and not serialization_truncated
            ),
        }

    @staticmethod
    def _wrap_graph_payload(payload: dict[str, Any]) -> str:
        return "[SOORIN_GRAPH_CONTEXT_JSON]\n" + json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ) + "\n[/SOORIN_GRAPH_CONTEXT_JSON]"

    @staticmethod
    def _subnet_counts(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for node in nodes:
            subnet = str(node.get("subnet") or "unknown")
            counts[subnet] = counts.get(subnet, 0) + 1
        return [
            {"subnet": subnet, "count": count}
            for subnet, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    def _finalize_graph_serialization(
        self,
        context: dict[str, Any],
        *,
        text: str,
        token_cap: int,
        included_nodes: int,
        included_edges: int,
        mode: str,
        serialization_truncated: bool,
        reason: str | None,
        target_ip: str = "",
        selected_peers: list[dict[str, Any]] | None = None,
    ) -> None:
        peers = selected_peers or []
        direction_counts = self._included_direction_counts(peers, target_ip)
        context.update(
            {
                "included_node_count": included_nodes,
                "included_edge_count": included_edges,
                "context_node_count": included_nodes,
                "context_edge_count": included_edges,
                "inbound_context_included": direction_counts["inbound"],
                "outbound_context_included": direction_counts["outbound"],
                "bidirectional_context_included": direction_counts["bidirectional"],
                "serialized_context_complete_for_retrieved_subset": not serialization_truncated,
                "serialized_context_truncated": serialization_truncated,
                "serialized_context_truncation_reason": reason,
                "context_truncated": serialization_truncated,
                "context_truncation_reason": reason,
                "context_truncation_reasons": [reason] if reason else [],
                "context_mode": mode,
                "aggregate_only_context": mode == "aggregate_only",
                "complete_for_user_request": bool(
                    context.get("requested_scope_complete", False) and not serialization_truncated
                ),
                "model_context_token_estimate": approx_tokens(text),
                "model_context_token_cap": token_cap,
                "model_input_graph_included": True,
            }
        )

    def _compose_node_summary_graph(
        self,
        package: CopilotContextPackage,
        token_budget: int,
        request_id: str,
    ) -> str:
        graph = package.graph
        if not graph:
            return ""
        context = graph.context
        subnets = [str(item) for item in context.get("subnet_distribution", ()) if item]
        subnet_limit = 12
        omitted_subnets = max(0, len(subnets) - subnet_limit)
        truncated = omitted_subnets > 0
        coverage = self._graph_coverage(
            context,
            serialization_truncated=truncated,
            reason="node_summary_subnet_top_k" if truncated else None,
        )
        payload = {
            "status": graph.status,
            "representation": "node_summary",
            "target_ip": context.get("target_ip"),
            "degree": context.get("degree", 0),
            "in_degree": context.get("in_degree", 0),
            "out_degree": context.get("out_degree", 0),
            "inbound_total": context.get("inbound_total", 0),
            "outbound_total": context.get("outbound_total", 0),
            "bidirectional_total": context.get("bidirectional_total", 0),
            "importance_or_centrality": context.get("importance"),
            "subnet_distribution": {
                "total": len(subnets),
                "top": subnets[:subnet_limit],
                "omitted": omitted_subnets,
            },
            "coverage": coverage,
            "limitations": list(context.get("limitations") or graph.limitations or []),
            "grounding_rules": GRAPH_GROUNDING_RULES,
        }
        text = self._wrap_graph_payload(payload)
        if approx_tokens(text) > token_budget:
            return ""
        self._finalize_graph_serialization(
            context,
            text=text,
            token_cap=token_budget,
            included_nodes=1 if context.get("node_found") else 0,
            included_edges=0,
            mode="aggregate_only",
            serialization_truncated=truncated,
            reason=coverage["serialized_context_truncation_reason"],
        )
        logger.info(
            "event=context_composer_graph_scope request_id=%s scope=node_summary graph_tokens=%s token_cap=%s included_nodes=%s included_edges=0 omitted_subnets=%s",
            request_id,
            approx_tokens(text),
            token_budget,
            context.get("included_node_count", 0),
            omitted_subnets,
        )
        return text

    def _compose_relationship_graph(
        self,
        package: CopilotContextPackage,
        token_budget: int,
        request_id: str,
    ) -> str:
        graph = package.graph
        if not graph:
            return ""
        context = graph.context
        relationship_edges = [dict(item) for item in context.get("edges", ())][:2]
        coverage = self._graph_coverage(context, serialization_truncated=False, reason=None)
        payload = {
            "status": graph.status,
            "representation": "direct_relationship",
            "targets": [context.get("source"), context.get("target")],
            "source_present": context.get("source_present", False),
            "target_present": context.get("target_present", False),
            "direction": {
                "source_to_target": context.get("forward_edge", False),
                "target_to_source": context.get("reverse_edge", False),
            },
            "relationship_status": context.get("relationship_status", context.get("relationship")),
            "relationship_metadata": relationship_edges,
            "coverage": coverage,
            "limitations": list(context.get("limitations") or graph.limitations or []),
            "grounding_rules": GRAPH_GROUNDING_RULES,
        }
        text = self._wrap_graph_payload(payload)
        if approx_tokens(text) > token_budget:
            return ""
        self._finalize_graph_serialization(
            context,
            text=text,
            token_cap=token_budget,
            included_nodes=int(bool(context.get("source_present"))) + int(bool(context.get("target_present"))),
            included_edges=len(relationship_edges),
            mode="direct_relationship",
            serialization_truncated=False,
            reason=None,
        )
        logger.info(
            "event=context_composer_graph_scope request_id=%s scope=relationship graph_tokens=%s token_cap=%s included_nodes=%s included_edges=%s",
            request_id,
            approx_tokens(text),
            token_budget,
            context.get("included_node_count", 0),
            len(relationship_edges),
        )
        return text

    def _compose_path_graph(
        self,
        package: CopilotContextPackage,
        token_budget: int,
        request_id: str,
    ) -> str:
        graph = package.graph
        if not graph:
            return ""
        context = graph.context
        path_nodes = list(context.get("path_nodes") or ())
        path_edges = list(context.get("path_edges") or ())
        coverage = self._graph_coverage(context, serialization_truncated=False, reason=None)
        payload = {
            "status": graph.status,
            "representation": "selected_path",
            "source": context.get("source_ip"),
            "destination": context.get("destination_ip"),
            "path_exists": context.get("path_exists", False),
            "hop_count": context.get("hop_count"),
            "path_nodes": path_nodes,
            "path_edges": path_edges,
            "coverage": coverage,
            "limitations": list(context.get("limitations") or graph.limitations or []),
            "grounding_rules": GRAPH_GROUNDING_RULES,
        }
        text = self._wrap_graph_payload(payload)
        if approx_tokens(text) > token_budget:
            return ""
        self._finalize_graph_serialization(
            context,
            text=text,
            token_cap=token_budget,
            included_nodes=len(path_nodes),
            included_edges=len(path_edges),
            mode="selected_path",
            serialization_truncated=False,
            reason=None,
        )
        logger.info(
            "event=context_composer_graph_scope request_id=%s scope=path graph_tokens=%s token_cap=%s included_nodes=%s included_edges=%s",
            request_id,
            approx_tokens(text),
            token_budget,
            len(path_nodes),
            len(path_edges),
        )
        return text

    @staticmethod
    def _peer_direction(node: dict[str, Any]) -> str:
        if node.get("bidirectional") or (node.get("inbound") and node.get("outbound")):
            return "bidirectional"
        if node.get("outbound"):
            return "outbound"
        if node.get("inbound"):
            return "inbound"
        return "observed"

    def _rank_graph_peers(
        self,
        context: dict[str, Any],
        peers: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        requested_direction = str(context.get("direction") or "both")
        target_ids = {str(item) for item in context.get("target_ips", ()) if item}

        def numeric(value: Any) -> float:
            try:
                return float(value or 0)
            except (TypeError, ValueError):
                return 0.0

        def priority(node: dict[str, Any]) -> tuple[Any, ...]:
            direction = self._peer_direction(node)
            return (
                0 if str(node.get("id")) in target_ids else 1,
                0 if node.get("shared") else 1,
                0 if direction == "bidirectional" else 1,
                0 if requested_direction == "both" or direction == requested_direction else 1,
                -numeric(node.get("importance", node.get("centrality"))),
                -numeric(node.get("degree")),
                str(node.get("subnet") or "unknown"),
                str(node.get("id") or ""),
            )

        ordered = sorted(peers, key=priority)
        if limit <= 0:
            return []
        if len(ordered) <= limit:
            return ordered
        priority_head = ordered[: max(1, limit // 2)]
        selected_ids = {str(item.get("id")) for item in priority_head}
        selected = list(priority_head)
        if len(selected) >= limit:
            return selected[:limit]
        seen_subnets = {str(item.get("subnet") or "unknown") for item in selected}
        for node in ordered:
            subnet = str(node.get("subnet") or "unknown")
            node_id = str(node.get("id"))
            if node_id not in selected_ids and subnet not in seen_subnets:
                selected.append(node)
                selected_ids.add(node_id)
                seen_subnets.add(subnet)
                if len(selected) >= limit:
                    return selected
        for node in ordered:
            node_id = str(node.get("id"))
            if node_id not in selected_ids:
                selected.append(node)
                selected_ids.add(node_id)
                if len(selected) >= limit:
                    break
        return selected

    def _compose_neighborhood_graph(
        self,
        package: CopilotContextPackage,
        token_budget: int,
        request_id: str,
    ) -> str:
        graph = package.graph
        if not graph:
            return ""
        context = graph.context
        scope = str(context.get("requested_scope") or context.get("scope") or "one_hop")
        target_ip = str(context.get("target_ip") or "")
        nodes = [dict(item) for item in context.get("nodes", ()) if item.get("id")]
        target_nodes = [item for item in nodes if str(item.get("id")) == target_ip]
        peers = [item for item in nodes if str(item.get("id")) != target_ip]
        default_limit = {"one_hop": 24, "two_hop": 36, "full_neighbors": 40}.get(scope, 24)
        configured_limit = max(0, self.settings.graph_context_max_enumerated_nodes - 1)
        if scope == "full_neighbors":
            configured_limit = min(
                configured_limit,
                self.settings.graph_full_enumeration_max_peers,
            )
        default_limit = min(default_limit, configured_limit)

        def build(limit: int) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
            selected = self._rank_graph_peers(context, peers, limit)
            compact_peers = [
                {
                    "id": str(node.get("id")),
                    "hop": int(node.get("hop", 1) or 1),
                    "direction": self._peer_direction(node),
                    "subnet": node.get("subnet"),
                    **({"importance": node.get("importance", node.get("centrality"))} if node.get("importance", node.get("centrality")) is not None else {}),
                    **({"degree": node.get("degree")} if node.get("degree") is not None else {}),
                }
                for node in selected
            ]
            model_omitted = max(0, len(peers) - len(selected))
            retrieval_omitted_nodes = max(
                0,
                int(context.get("candidate_node_count", len(nodes)))
                - int(context.get("retrieved_node_count", len(nodes))),
            )
            truncated = model_omitted > 0
            reason = f"{scope}_peer_top_k" if truncated else None
            coverage = self._graph_coverage(
                context,
                serialization_truncated=truncated,
                reason=reason,
            )
            hop_counts: dict[str, int] = {}
            for node in peers:
                hop = str(int(node.get("hop", 1) or 1))
                hop_counts[hop] = hop_counts.get(hop, 0) + 1
            payload = {
                "status": graph.status,
                "representation": f"{scope}_summary",
                "target_ip": target_ip,
                "direction": context.get("direction", "both"),
                "depth": context.get("depth", 1),
                "totals": {
                    "inbound": context.get("inbound_total", 0),
                    "outbound": context.get("outbound_total", 0),
                    "bidirectional": context.get("bidirectional_total", 0),
                    "candidate_nodes": context.get("candidate_node_count", 0),
                    "retrieved_nodes": context.get("retrieved_node_count", context.get("returned_node_count", 0)),
                    "candidate_edges": context.get("candidate_edge_count", 0),
                    "retrieved_edges": context.get("retrieved_edge_count", context.get("returned_edge_count", 0)),
                },
                "hop_counts": hop_counts,
                "subnet_distribution": self._subnet_counts(peers),
                "top_peers": compact_peers,
                "serialization_counts": {
                    "retrieved_peer_count": len(peers),
                    "included_peer_count": len(selected),
                    "omitted_peer_count": model_omitted,
                    "retrieval_omitted_node_count": retrieval_omitted_nodes,
                    "serialized_edge_count": 0,
                },
                "coverage": coverage,
                "continuation_guidance": (
                    "Ask for a narrower direction, subnet, or specific peer subset."
                    if scope == "full_neighbors" and (model_omitted or retrieval_omitted_nodes)
                    else None
                ),
                "limitations": list(context.get("limitations") or graph.limitations or []),
                "grounding_rules": GRAPH_GROUNDING_RULES,
            }
            return self._wrap_graph_payload(payload), payload, selected

        limit = min(default_limit, len(peers))
        text, payload, selected = build(limit)
        while limit > 0 and approx_tokens(text) > token_budget:
            limit -= 1
            text, payload, selected = build(limit)
        if approx_tokens(text) > token_budget:
            return ""
        coverage = payload["coverage"]
        self._finalize_graph_serialization(
            context,
            text=text,
            token_cap=token_budget,
            included_nodes=len(target_nodes[:1]) + len(selected),
            included_edges=0,
            mode=f"{scope}_summary",
            serialization_truncated=coverage["serialized_context_truncated"],
            reason=coverage["serialized_context_truncation_reason"],
            target_ip=target_ip,
            selected_peers=selected,
        )
        context["model_context_omitted_peer_count"] = payload["serialization_counts"]["omitted_peer_count"]
        logger.info(
            "event=context_composer_graph_scope request_id=%s scope=%s graph_tokens=%s token_cap=%s retrieved_peers=%s included_peers=%s omitted_peers=%s included_edges=0",
            request_id,
            scope,
            approx_tokens(text),
            token_budget,
            payload["serialization_counts"]["retrieved_peer_count"],
            payload["serialization_counts"]["included_peer_count"],
            payload["serialization_counts"]["omitted_peer_count"],
        )
        return text

    def _compose_comparison_graph(
        self,
        package: CopilotContextPackage,
        *,
        token_budget: int,
        request_id: str,
    ) -> str:
        """Serialize comparison aggregates and bounded peer examples, never the raw subgraph."""
        graph = package.graph
        if not graph:
            return ""
        context = graph.context or {}
        entity_a = dict(context.get("entity_a") or {})
        entity_b = dict(context.get("entity_b") or {})
        entity_a_ip = str(entity_a.get("ip") or "entity_a")
        entity_b_ip = str(entity_b.get("ip") or "entity_b")
        a_peers = tuple(str(item) for item in entity_a.get("peers_retrieved", ()) if item)
        b_peers = tuple(str(item) for item in entity_b.get("peers_retrieved", ()) if item)
        a_peer_set = set(a_peers)
        b_peer_set = set(b_peers)
        shared = tuple(str(item) for item in context.get("shared_peers_retrieved", ()) if item)
        a_distinct = tuple(item for item in a_peers if item not in b_peer_set)
        b_distinct = tuple(item for item in b_peers if item not in a_peer_set)
        raw_nodes = len(context.get("nodes") or ())
        raw_edges = len(context.get("edges") or ())
        direct_relationship = self._comparison_direct_relationship_block(
            context,
            entity_a_ip,
            entity_b_ip,
        )

        def entity_summary(source: dict[str, Any], top_k: int) -> dict[str, Any]:
            subnets = [str(item) for item in source.get("subnets", ()) if item]
            return {
                "ip": source.get("ip"),
                "present": source.get("present", False),
                "inbound_total": source.get("inbound_total", 0),
                "outbound_total": source.get("outbound_total", 0),
                "bidirectional_total": source.get("bidirectional_total", 0),
                "total_peer_count": source.get("total_peer_count", 0),
                "retrieval_truncated": source.get("retrieval_truncated", False),
                "subnet_count": len(subnets),
                "top_subnets": subnets[:top_k],
            }

        def build(top_k: int, *, include_optional: bool = True) -> tuple[str, dict[str, Any], set[str]]:
            top_shared = list(shared[:top_k])
            top_a_distinct = list(a_distinct[:top_k])
            top_b_distinct = list(b_distinct[:top_k])
            referenced_peers = {*top_shared, *top_a_distinct, *top_b_distinct}
            omitted = bool(
                len(shared) > len(top_shared)
                or int(context.get("entity_a_unique_peer_total", 0)) > len(top_a_distinct)
                or int(context.get("entity_b_unique_peer_total", 0)) > len(top_b_distinct)
                or entity_a.get("retrieval_truncated")
                or entity_b.get("retrieval_truncated")
            )
            coverage = {
                "retrieval_complete": context.get("retrieval_complete", False),
                "retrieval_truncated": context.get("retrieval_truncated", False),
                "retrieval_truncation_reason": context.get("retrieval_truncation_reason"),
                "requested_scope_complete": context.get("requested_scope_complete", False),
                "serialized_context_complete_for_retrieved_subset": not omitted,
                "serialized_context_truncated": omitted,
                "serialized_context_truncation_reason": "comparison_summary_top_k" if omitted else None,
                "complete_for_user_request": bool(
                    context.get("requested_scope_complete", False) and not omitted
                ),
            }
            subnet_source = dict(context.get("subnet_comparison") or {})
            subnet_differences = {
                key: {
                    "total": len(value) if isinstance(value, list) else 0,
                    "top": value[:top_k] if isinstance(value, list) else [],
                }
                for key, value in subnet_source.items()
            }
            payload = {
                "status": graph.status,
                "representation": "comparison_summary",
                "target_ips": [entity_a_ip, entity_b_ip],
                "requested_scope": context.get("requested_scope", context.get("scope", "multi_entity_comparison")),
                "coverage": coverage,
                "direct_relationship": direct_relationship,
                "entities": {
                    entity_a_ip: entity_summary(entity_a, top_k),
                    entity_b_ip: entity_summary(entity_b, top_k),
                },
                "serialization": {
                    "policy": "comparison_summary_top_k",
                    "top_k": top_k,
                    "direct_relationship_preserved": True,
                    "retrieved_node_records": raw_nodes,
                    "retrieved_edge_records": raw_edges,
                    "serialized_peer_references": len(referenced_peers),
                    "omitted_peer_references": max(
                        0,
                        len(shared) + len(a_distinct) + len(b_distinct) - len(referenced_peers),
                    ),
                    "omitted_node_records": max(0, raw_nodes - 2 - len(referenced_peers)),
                    "neighborhood_edge_records_serialized": 0,
                    "neighborhood_edge_records_omitted": raw_edges,
                    "serialized_edge_records": 0,
                    "omitted_edge_records": raw_edges,
                },
                "limitations": list(context.get("limitations") or graph.limitations or []),
                "grounding_rules": GRAPH_GROUNDING_RULES,
            }
            if include_optional:
                payload.update(
                    {
                        "peer_comparison": {
                            "shared_peer_count": context.get("shared_peer_total", 0),
                            "shared_peer_retrieved_count": len(shared),
                            "shared_peer_omitted_from_model": max(0, len(shared) - len(top_shared)),
                            "top_shared_peers": top_shared,
                            "entity_a_distinct_peer_count": context.get("entity_a_unique_peer_total", 0),
                            "entity_a_distinct_peer_retrieved_count": len(a_distinct),
                            "entity_a_distinct_peer_omitted_from_model": max(
                                0, len(a_distinct) - len(top_a_distinct)
                            ),
                            "top_entity_a_distinct_peers": top_a_distinct,
                            "entity_b_distinct_peer_count": context.get("entity_b_unique_peer_total", 0),
                            "entity_b_distinct_peer_retrieved_count": len(b_distinct),
                            "entity_b_distinct_peer_omitted_from_model": max(
                                0, len(b_distinct) - len(top_b_distinct)
                            ),
                            "top_entity_b_distinct_peers": top_b_distinct,
                        },
                        "degree_differences": context.get("degree_comparison", {}),
                        "centrality_differences": context.get(
                            "centrality_comparison",
                            {"available": False, "limitation": "Centrality was not calculated for this comparison."},
                        ),
                        "subnet_distribution_differences": subnet_differences,
                    }
                )
            else:
                payload["coverage"]["serialized_context_truncated"] = True
                payload["coverage"]["serialized_context_truncation_reason"] = "comparison_optional_details_omitted"
                payload["coverage"]["serialized_context_complete_for_retrieved_subset"] = False
                payload["coverage"]["complete_for_user_request"] = False
            text = "[SOORIN_GRAPH_CONTEXT_JSON]\n" + json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ) + "\n[/SOORIN_GRAPH_CONTEXT_JSON]"
            return text, payload, referenced_peers

        selected_top_k = COMPARISON_CONTEXT_TOP_K
        text, payload, referenced_peers = build(selected_top_k)
        while selected_top_k > 0 and approx_tokens(text) > token_budget:
            selected_top_k -= 1
            text, payload, referenced_peers = build(selected_top_k)
        if approx_tokens(text) > token_budget:
            text, payload, referenced_peers = build(0, include_optional=False)
        if approx_tokens(text) > token_budget:
            return ""

        serialized_truncated = bool(payload["coverage"]["serialized_context_truncated"])
        context.update(
            {
                "included_node_count": 2 + len(referenced_peers),
                "included_edge_count": 0,
                "context_node_count": 2 + len(referenced_peers),
                "context_edge_count": 0,
                "serialized_context_complete_for_retrieved_subset": not serialized_truncated,
                "serialized_context_truncated": serialized_truncated,
                "serialized_context_truncation_reason": payload["coverage"]["serialized_context_truncation_reason"],
                "context_truncated": serialized_truncated,
                "context_truncation_reason": payload["coverage"]["serialized_context_truncation_reason"],
                "context_mode": "comparison_summary",
                "complete_for_user_request": payload["coverage"]["complete_for_user_request"],
                "model_context_token_estimate": approx_tokens(text),
                "model_context_token_cap": token_budget,
                "direct_relationship_preserved": True,
                "required_direct_relationship_preserved": True,
                "required_graph_fact_count": 1,
                "neighborhood_edge_records_serialized": payload["serialization"]["neighborhood_edge_records_serialized"],
                "neighborhood_edge_records_omitted": payload["serialization"]["neighborhood_edge_records_omitted"],
                "model_context_omitted_peer_count": max(
                    0,
                    int(context.get("shared_peer_total", 0))
                    + int(context.get("entity_a_unique_peer_total", 0))
                    + int(context.get("entity_b_unique_peer_total", 0))
                    - len(referenced_peers),
                ),
                "model_input_graph_included": True,
            }
        )
        logger.info(
            "event=context_composer_graph_comparison request_id=%s raw_nodes=%s raw_edges=%s serialized_peer_references=%s direct_relationship_preserved=%s neighborhood_edges_serialized=0 top_k=%s context_tokens=%s token_budget=%s truncated=%s",
            request_id,
            raw_nodes,
            raw_edges,
            len(referenced_peers),
            True,
            selected_top_k,
            approx_tokens(text),
            token_budget,
            serialized_truncated,
        )
        return text

    @staticmethod
    def _comparison_direct_relationship_block(
        context: dict[str, Any],
        source_ip: str,
        target_ip: str,
    ) -> dict[str, Any]:
        direct = dict(context.get("direct_relationship") or {})
        forward = bool(direct.get("a_to_b", direct.get("forward_edge", False)))
        reverse = bool(direct.get("b_to_a", direct.get("reverse_edge", False)))
        if forward and reverse:
            status = "bidirectional"
        elif forward:
            status = "source_to_target"
        elif reverse:
            status = "target_to_source"
        else:
            status = "no_direct_relationship"
        return {
            "source": source_ip,
            "target": target_ip,
            "forward_edge": forward,
            "reverse_edge": reverse,
            "status": status,
            "relationship_status": direct.get("relationship_status", status),
            "preserved": True,
        }

    def _compose_compact_graph(
        self,
        package: CopilotContextPackage,
        *,
        token_budget: int,
        request_id: str,
    ) -> str:
        graph = package.graph
        if not graph:
            return ""
        context = graph.context
        target_ip = str(context.get("target_ip") or "")
        all_nodes = [dict(node) for node in context.get("nodes", []) if node.get("id")]
        target_nodes = [node for node in all_nodes if str(node.get("id")) == target_ip]
        peers = [node for node in all_nodes if str(node.get("id")) != target_ip]
        all_edges = [dict(edge) for edge in (context.get("edges") or context.get("path_edges") or [])]

        def edge_weight(edge: dict[str, Any]) -> float:
            try:
                return float(edge.get("weight", 1) or 1)
            except (TypeError, ValueError):
                return 1.0

        exceptional_peers = {
            endpoint
            for edge in all_edges
            if edge_weight(edge) > 1
            for endpoint in (str(edge.get("source")), str(edge.get("target")))
            if endpoint != target_ip
        }

        def peer_priority(node: dict[str, Any]) -> tuple[int, str]:
            node_id = str(node.get("id"))
            if node.get("bidirectional") or (node.get("inbound") and node.get("outbound")):
                rank = 0
            elif node.get("outbound"):
                rank = 1
            elif node_id in exceptional_peers:
                rank = 2
            elif node.get("inbound"):
                rank = 3
            else:
                rank = 4
            return rank, node_id

        peers.sort(key=peer_priority)
        rank_by_id = {str(node.get("id")): index for index, node in enumerate(peers)}
        all_edges.sort(
            key=lambda edge: (
                -edge_weight(edge),
                min(
                    rank_by_id.get(str(edge.get("source")), len(peers)),
                    rank_by_id.get(str(edge.get("target")), len(peers)),
                ),
                str(edge.get("source")),
                str(edge.get("target")),
            )
        )

        subnet_groups: dict[str, dict[str, int]] = {}
        for node in peers:
            subnet = str(node.get("subnet") or "unknown")
            group = subnet_groups.setdefault(
                subnet,
                {"peer_count": 0, "inbound": 0, "outbound": 0, "bidirectional": 0},
            )
            group["peer_count"] += 1
            group["inbound"] += int(bool(node.get("inbound")))
            group["outbound"] += int(bool(node.get("outbound")))
            group["bidirectional"] += int(
                bool(node.get("bidirectional") or (node.get("inbound") and node.get("outbound")))
            )

        def build(peer_limit: int, edge_limit: int) -> tuple[str, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
            selected_peers = peers[:peer_limit]
            selected_ids = {target_ip, *(str(node.get("id")) for node in selected_peers)}
            eligible_edges = [
                edge
                for edge in all_edges
                if str(edge.get("source")) in selected_ids and str(edge.get("target")) in selected_ids
            ]
            selected_edges = eligible_edges[:edge_limit]
            selected_nodes = [*target_nodes[:1], *selected_peers]
            direction_counts = self._included_direction_counts(selected_nodes, target_ip)
            nodes_complete = len(selected_nodes) == len(all_nodes)
            edges_complete = len(selected_edges) == len(all_edges)
            serialized_complete = nodes_complete and edges_complete
            reasons = []
            if not nodes_complete:
                reasons.append("graph_budget_peer_limit")
            if not edges_complete:
                reasons.append("graph_budget_edge_limit")
            coverage = {
                "retrieval_complete": context.get("retrieval_complete", False),
                "retrieval_truncated": context.get("retrieval_truncated", False),
                "retrieval_truncation_reason": context.get("retrieval_truncation_reason"),
                "serialized_context_complete_for_retrieved_subset": serialized_complete,
                "serialized_context_truncated": not serialized_complete,
                "serialized_context_truncation_reason": reasons[0] if reasons else None,
                "requested_scope_complete": context.get("requested_scope_complete", False),
                "complete_for_user_request": bool(
                    context.get("requested_scope_complete", False) and serialized_complete
                ),
            }
            compact_peers = []
            for node in selected_peers:
                direction = (
                    "bidirectional"
                    if node.get("bidirectional") or (node.get("inbound") and node.get("outbound"))
                    else "outbound"
                    if node.get("outbound")
                    else "inbound"
                    if node.get("inbound")
                    else "observed"
                )
                compact_peers.append(
                    {
                        "id": str(node.get("id")),
                        "direction": direction,
                        "subnet": node.get("subnet"),
                    }
                )
            payload = {
                "status": graph.status,
                "representation": "compact",
                "target_ip": target_ip,
                "target_ips": context.get("target_ips", [target_ip] if target_ip else []),
                "requested_scope": context.get("requested_scope", context.get("scope", "none")),
                "direction": context.get("direction", "none"),
                "depth": context.get("depth", 0),
                "coverage": coverage,
                "counts": {
                    "candidate_nodes": context.get("candidate_node_count", 0),
                    "returned_nodes": context.get("retrieved_node_count", context.get("returned_node_count", 0)),
                    "serialized_nodes": len(selected_nodes),
                    "candidate_edges": context.get("candidate_edge_count", 0),
                    "returned_edges": context.get("retrieved_edge_count", context.get("returned_edge_count", 0)),
                    "serialized_edges": len(selected_edges),
                },
                "relationships": {
                    "inbound": {
                        "total": context.get("inbound_total", 0),
                        "returned": context.get("inbound_retrieved", context.get("inbound_returned", 0)),
                        "included": direction_counts["inbound"],
                    },
                    "outbound": {
                        "total": context.get("outbound_total", 0),
                        "returned": context.get("outbound_retrieved", context.get("outbound_returned", 0)),
                        "included": direction_counts["outbound"],
                    },
                    "bidirectional": {
                        "total": context.get("bidirectional_total", 0),
                        "returned": context.get("bidirectional_retrieved", context.get("bidirectional_returned", 0)),
                        "included": direction_counts["bidirectional"],
                    },
                },
                "subnet_groups": [
                    {"subnet": subnet, **counts}
                    for subnet, counts in sorted(subnet_groups.items())
                ],
                "peers": compact_peers,
                "edges": [
                    [edge.get("source"), edge.get("target"), edge.get("weight", 1)]
                    for edge in selected_edges
                ],
                "limitations": list(context.get("limitations") or graph.limitations or []),
                "grounding_rules": GRAPH_GROUNDING_RULES,
            }
            text = "[SOORIN_GRAPH_CONTEXT_JSON]\n" + json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ) + "\n[/SOORIN_GRAPH_CONTEXT_JSON]"
            return text, payload, selected_nodes, selected_edges

        all_peer_count = len(peers)
        text, payload, selected_nodes, selected_edges = build(all_peer_count, len(all_edges))
        if approx_tokens(text) > token_budget:
            low, high, best_edges = 0, len(all_edges), 0
            while low <= high:
                middle = (low + high) // 2
                trial_text, _, _, _ = build(all_peer_count, middle)
                if approx_tokens(trial_text) <= token_budget:
                    best_edges = middle
                    low = middle + 1
                else:
                    high = middle - 1
            text, payload, selected_nodes, selected_edges = build(all_peer_count, best_edges)

        if approx_tokens(text) > token_budget:
            low, high, best_peers = 0, all_peer_count, -1
            while low <= high:
                middle = (low + high) // 2
                trial_text, _, _, _ = build(middle, 0)
                if approx_tokens(trial_text) <= token_budget:
                    best_peers = middle
                    low = middle + 1
                else:
                    high = middle - 1
            if best_peers < 0:
                return ""
            low, high, best_edges = 0, len(all_edges), 0
            while low <= high:
                middle = (low + high) // 2
                trial_text, _, _, _ = build(best_peers, middle)
                if approx_tokens(trial_text) <= token_budget:
                    best_edges = middle
                    low = middle + 1
                else:
                    high = middle - 1
            text, payload, selected_nodes, selected_edges = build(best_peers, best_edges)

        coverage = payload["coverage"]
        direction_counts = self._included_direction_counts(selected_nodes, target_ip)
        context.update(
            {
                "included_node_count": len(selected_nodes),
                "included_edge_count": len(selected_edges),
                "context_node_count": len(selected_nodes),
                "context_edge_count": len(selected_edges),
                "inbound_context_included": direction_counts["inbound"],
                "outbound_context_included": direction_counts["outbound"],
                "bidirectional_context_included": direction_counts["bidirectional"],
                "serialized_context_complete_for_retrieved_subset": coverage["serialized_context_complete_for_retrieved_subset"],
                "serialized_context_truncated": coverage["serialized_context_truncated"],
                "serialized_context_truncation_reason": coverage["serialized_context_truncation_reason"],
                "context_truncated": coverage["serialized_context_truncated"],
                "context_truncation_reason": coverage["serialized_context_truncation_reason"],
                "context_mode": "compact_enumerated",
                "aggregate_only_context": False,
                "complete_for_user_request": coverage["complete_for_user_request"],
                "model_input_graph_included": True,
                "model_input_graph_complete": coverage["serialized_context_complete_for_retrieved_subset"],
            }
        )
        logger.info(
            "event=context_composer_graph_budgeted request_id=%s requested_scope=%s graph_budget_tokens=%s graph_tokens=%s returned_nodes=%s included_nodes=%s returned_edges=%s included_edges=%s retrieval_complete=%s serialization_complete=%s complete_for_user_request=%s",
            request_id,
            payload["requested_scope"],
            token_budget,
            approx_tokens(text),
            payload["counts"]["returned_nodes"],
            payload["counts"]["serialized_nodes"],
            payload["counts"]["returned_edges"],
            payload["counts"]["serialized_edges"],
            coverage["retrieval_complete"],
            coverage["serialized_context_complete_for_retrieved_subset"],
            coverage["complete_for_user_request"],
        )
        return text

    def _select_context_records(self, context: dict[str, Any]) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str]]:
        nodes = list(context.get("nodes") or [])
        edges = list(context.get("edges") or context.get("path_edges") or [])
        scope = str(context.get("scope", "node_summary"))
        target_ip = str(context.get("target_ip") or "")
        if scope == "node_summary":
            return [node for node in nodes if str(node.get("id") or "") == target_ip][:1], [], []
        node_limit = self.settings.graph_context_max_enumerated_nodes
        edge_limit = self.settings.graph_context_max_enumerated_edges
        if scope == "full_neighbors" and self._matching_peer_count(context, str(context.get("direction", "both"))) <= self.settings.graph_full_enumeration_max_peers:
            node_limit, edge_limit = max(node_limit, len(nodes)), max(edge_limit, len(edges))
        if scope == "path":
            node_limit, edge_limit = max(node_limit, len(nodes)), max(edge_limit, len(edges))
        ordered = [*[node for node in nodes if str(node.get("id") or "") == target_ip], *[node for node in nodes if str(node.get("id") or "") != target_ip]]
        selected_nodes = ordered[:node_limit]
        selected_ids = {str(node.get("id")) for node in selected_nodes if node.get("id")}
        eligible_edges = [edge for edge in edges if str(edge.get("source")) in selected_ids and str(edge.get("target")) in selected_ids]
        selected_edges = eligible_edges[:edge_limit]
        reasons: list[str] = []
        if len(selected_nodes) < len(nodes):
            reasons.append("graph_context_node_limit")
        if len(selected_edges) < len(eligible_edges):
            reasons.append("graph_context_edge_limit")
        return selected_nodes, selected_edges, reasons

    @staticmethod
    def _matching_peer_count(context: dict[str, Any], direction: str) -> int:
        if direction == "inbound":
            return int(context.get("inbound_retrieved", context.get("inbound_returned", 0)) or 0)
        if direction == "outbound":
            return int(context.get("outbound_retrieved", context.get("outbound_returned", 0)) or 0)
        return len({str(node.get("id")) for node in context.get("nodes", []) if node.get("hop") != 0 and node.get("id")})

    @staticmethod
    def _included_direction_counts(nodes: list[dict[str, object]], target_ip: object) -> dict[str, int]:
        target = str(target_ip or "")
        inbound = {str(node.get("id")) for node in nodes if node.get("id") and str(node.get("id")) != target and bool(node.get("inbound"))}
        outbound = {str(node.get("id")) for node in nodes if node.get("id") and str(node.get("id")) != target and bool(node.get("outbound"))}
        return {"inbound": len(inbound), "outbound": len(outbound), "bidirectional": len(inbound & outbound)}
