"""Compose bounded, deterministic model-facing provider context."""

from __future__ import annotations

import json
import logging
from typing import Any

from src.config.settings import Settings, get_settings
from src.core.context.models import CopilotContextPackage, approx_tokens


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
        "description": "Observed communication topology.",
        "limitation": "Does not by itself prove protocol purpose, trust, service dependency, successful authentication, compromise, routing capability, or attack paths; coverage may be partial.",
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

COMPACT_PRODUCT_TARGET_TOKENS = 384
MIN_EXHAUSTIVE_GRAPH_TOKENS = 512


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
            "fusion": "",
        }
        self.last_inclusion: dict[str, tuple[bool, str | None]] = {}
        self.last_representation: dict[str, str] = {}
        self.required_context_missing = False
        self.last_budget: dict[str, int] = {}
        self.last_knowledge_included_count = 0

    def compose(
        self,
        package: CopilotContextPackage,
        *,
        request_id: str = "",
        base_input_tokens: int = 0,
        reserved_output_tokens: int | None = None,
    ) -> str:
        profile_sections = self._compose_json_sections(package.asset_profiles, "ASSET_PROFILE_JSON")
        detection_sections = self._compose_json_sections(package.detections, "ASSET_DETECTION_JSON")
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
        self.last_budget = {
            "base_input_tokens": base_input_tokens,
            "max_dynamic_tokens": max_dynamic_tokens,
            "reserved_output_tokens": output_reserve,
            "calibrated_dynamic_capacity": calibrated_capacity,
        }
        if self._is_exhaustive_graph_request(package):
            return self._compose_exhaustive_graph_context(
                package,
                profile_sections,
                detection_sections,
                max_dynamic_tokens=max_dynamic_tokens,
                request_id=request_id,
            )

        self.last_inclusion = {}
        self.last_representation = {}
        self.last_knowledge_included_count = 0
        graph_candidate = self._compose_graph(package, request_id=request_id)
        knowledge_candidate = self._compose_knowledge(
            package,
            token_budget=min(self.settings.rag_max_context_tokens, max_dynamic_tokens),
        )
        self.last_inclusion["knowledge"] = (False, "not_included")
        self.last_representation["knowledge"] = "excluded"
        provisional = self._compose_provider_manifest(package, graph_included=bool(graph_candidate))
        used_tokens = approx_tokens(provisional)
        included_profiles: list[str] = []
        included_detections: list[str] = []
        for provider_name, ip, section in [*profile_sections, *detection_sections]:
            key = f"{provider_name}:{ip}"
            section_tokens = approx_tokens(section)
            if used_tokens + section_tokens <= max_dynamic_tokens:
                used_tokens += section_tokens
                self.last_inclusion[key] = (True, None)
                self.last_representation[key] = "full"
                (included_profiles if provider_name == "asset_profile" else included_detections).append(section)
            else:
                self.last_inclusion[key] = (False, "global_context_limit")
                self.last_representation[key] = "excluded"
                logger.warning(
                    "event=product_context_not_included request_id=%s provider=%s target_ip=%s raw_json_approx_tokens=%s reason=global_context_limit raw_payload_preserved=true",
                    request_id,
                    provider_name,
                    ip,
                    section_tokens,
                )

        graph_text = graph_candidate
        graph_included = False
        if graph_text:
            graph_tokens = approx_tokens(graph_text)
            if used_tokens + graph_tokens <= max_dynamic_tokens:
                graph_included = True
                used_tokens += graph_tokens
            else:
                logger.warning(
                    "event=graph_context_not_included request_id=%s context_approx_tokens=%s reason=global_context_limit",
                    request_id,
                    graph_tokens,
                )
                graph_text = ""
        self.last_inclusion["graph"] = (graph_included, None if graph_included else "global_context_limit")
        self.last_representation["graph"] = "full" if graph_included else "excluded"

        knowledge_text = ""
        if knowledge_candidate:
            knowledge_tokens = approx_tokens(knowledge_candidate)
            if used_tokens + knowledge_tokens <= max_dynamic_tokens:
                knowledge_text = knowledge_candidate
                used_tokens += knowledge_tokens
                self.last_inclusion["knowledge"] = (True, None)
                self.last_representation["knowledge"] = (
                    "full"
                    if package.knowledge and self.last_knowledge_included_count == package.knowledge.included_count
                    else "compact"
                )
            else:
                self.last_knowledge_included_count = 0
                self.last_inclusion["knowledge"] = (False, "global_context_limit")
                logger.warning(
                    "event=knowledge_context_not_included request_id=%s context_approx_tokens=%s reason=global_context_limit",
                    request_id,
                    knowledge_tokens,
                )

        manifest = self._compose_provider_manifest(package, graph_included=graph_included)
        profile_text = "\n\n".join(included_profiles)
        detection_text = "\n\n".join(included_detections)
        parts = [part for part in (manifest, profile_text, detection_text, graph_text, knowledge_text) if part]
        text = "\n\n".join(parts)
        self.last_parts = {
            "status": manifest,
            "asset_profile": profile_text,
            "detection": detection_text,
            "graph": graph_text,
            "knowledge": knowledge_text,
            "fusion": "",
        }
        logger.info(
            "event=context_composed request_id=%s providers=%s manifest_chars=%s asset_profile_chars=%s detection_chars=%s graph_chars=%s knowledge_chars=%s total_dynamic_chars=%s total_dynamic_approx_tokens=%s max_dynamic_tokens=%s",
            request_id,
            ",".join(name for name, value in (("manifest", manifest), ("asset_profile", profile_text), ("detection", detection_text), ("graph", graph_text), ("knowledge", knowledge_text)) if value),
            len(manifest),
            len(profile_text),
            len(detection_text),
            len(graph_text),
            len(knowledge_text),
            len(text),
            approx_tokens(text),
            max_dynamic_tokens,
        )
        return text

    @staticmethod
    def _is_exhaustive_graph_request(package: CopilotContextPackage) -> bool:
        if not package.graph:
            return False
        context = package.graph.context or {}
        return bool(
            context.get("scope") == "full_neighbors"
            or context.get("requested_scope") == "full_neighbors"
            or context.get("exhaustive_connections_requested")
        )

    def _compose_exhaustive_graph_context(
        self,
        package: CopilotContextPackage,
        profile_sections: list[tuple[str, str, str]],
        detection_sections: list[tuple[str, str, str]],
        *,
        max_dynamic_tokens: int,
        request_id: str,
    ) -> str:
        """Allocate exhaustive graph context before narrative provider detail."""
        self.last_inclusion = {}
        self.last_representation = {}
        self.last_knowledge_included_count = 0
        self.last_inclusion["knowledge"] = (False, "not_included")
        self.last_representation["knowledge"] = "excluded"
        result_lookup = {
            ("asset_profile", item.ip): item for item in package.asset_profiles
        }
        result_lookup.update({("detection", item.ip): item for item in package.detections})
        candidates: list[dict[str, str]] = []
        for provider_name, ip, full_section in [*profile_sections, *detection_sections]:
            key = f"{provider_name}:{ip}"
            compact_section = self._compact_product_section(
                result_lookup[(provider_name, ip)],
                provider_name,
                ip,
                token_budget=COMPACT_PRODUCT_TARGET_TOKENS,
            )
            candidates.append(
                {
                    "provider": provider_name,
                    "ip": ip,
                    "key": key,
                    "full": full_section,
                    "compact": compact_section,
                }
            )
            self.last_inclusion[key] = (True, "route_aware_compaction")
            self.last_representation[key] = "compact"

        provisional_manifest = self._compose_provider_manifest(package, graph_included=True)
        compact_product_tokens = sum(approx_tokens(item["compact"]) for item in candidates)
        graph_budget = min(
            max(0, int(self.settings.graph_max_context_tokens)),
            max(
                0,
                max_dynamic_tokens
                - approx_tokens(provisional_manifest)
                - compact_product_tokens,
            ),
        )
        self.last_budget.update(
            {
                "manifest_tokens": approx_tokens(provisional_manifest),
                "compact_product_tokens": compact_product_tokens,
                "graph_budget_tokens": graph_budget,
            }
        )

        graph_text = ""
        graph_included = False
        if graph_budget >= MIN_EXHAUSTIVE_GRAPH_TOKENS:
            graph_text = self._compose_graph(
                package,
                request_id=request_id,
                token_budget=graph_budget,
            )
            graph_included = bool(graph_text) and approx_tokens(graph_text) <= graph_budget

        if not graph_included:
            self._mark_graph_excluded(package, "insufficient_global_context_budget")
            self.required_context_missing = True
            self.last_inclusion["graph"] = (False, "insufficient_global_context_budget")
            self.last_representation["graph"] = "excluded"
            logger.error(
                "event=required_graph_context_not_included request_id=%s requested_scope=full_neighbors graph_budget_tokens=%s max_dynamic_tokens=%s safe_failure=true",
                request_id,
                graph_budget,
                max_dynamic_tokens,
            )
        else:
            self.last_inclusion["graph"] = (True, None)
            self.last_representation["graph"] = (
                "compact" if (package.graph and package.graph.context.get("context_mode") == "compact_enumerated") else "full"
            )

        selected_sections = {item["key"]: item["compact"] for item in candidates}
        manifest = self._compose_provider_manifest(package, graph_included=graph_included)
        for item in candidates:
            key = item["key"]
            trial_sections = dict(selected_sections)
            trial_sections[key] = item["full"]
            self.last_representation[key] = "full"
            self.last_inclusion[key] = (True, None)
            trial_manifest = self._compose_provider_manifest(package, graph_included=graph_included)
            trial_text = "\n\n".join(
                part
                for part in (
                    trial_manifest,
                    *trial_sections.values(),
                    graph_text,
                )
                if part
            )
            if approx_tokens(trial_text) <= max_dynamic_tokens:
                selected_sections = trial_sections
                manifest = trial_manifest
            else:
                self.last_representation[key] = "compact"
                self.last_inclusion[key] = (True, "route_aware_compaction")

        manifest = self._compose_provider_manifest(package, graph_included=graph_included)
        profile_text = "\n\n".join(
            selected_sections[item["key"]]
            for item in candidates
            if item["provider"] == "asset_profile"
        )
        detection_text = "\n\n".join(
            selected_sections[item["key"]]
            for item in candidates
            if item["provider"] == "detection"
        )
        limitation = ""
        if self.required_context_missing:
            limitation = (
                "[SOORIN_CONTEXT_LIMITATION]\n"
                "The requested current exhaustive graph evidence could not fit safely. "
                "Do not answer from previous assistant claims or stale peer lists.\n"
                "[/SOORIN_CONTEXT_LIMITATION]"
            )
        parts = [part for part in (manifest, profile_text, detection_text, graph_text, limitation) if part]
        text = "\n\n".join(parts)

        if graph_included and approx_tokens(text) > max_dynamic_tokens:
            overflow = approx_tokens(text) - max_dynamic_tokens
            reduced_budget = max(
                MIN_EXHAUSTIVE_GRAPH_TOKENS,
                graph_budget - overflow - 64,
            )
            graph_text = self._compose_graph(
                package,
                request_id=request_id,
                token_budget=reduced_budget,
            )
            manifest = self._compose_provider_manifest(package, graph_included=bool(graph_text))
            parts = [part for part in (manifest, profile_text, detection_text, graph_text) if part]
            text = "\n\n".join(parts)

        if graph_included and (not graph_text or approx_tokens(text) > max_dynamic_tokens):
            self._mark_graph_excluded(package, "insufficient_global_context_budget")
            self.required_context_missing = True
            graph_text = ""
            self.last_inclusion["graph"] = (False, "insufficient_global_context_budget")
            self.last_representation["graph"] = "excluded"
            limitation = (
                "[SOORIN_CONTEXT_LIMITATION]\n"
                "The requested current exhaustive graph evidence could not fit safely. "
                "Do not answer from previous assistant claims or stale peer lists.\n"
                "[/SOORIN_CONTEXT_LIMITATION]"
            )
            manifest = self._compose_provider_manifest(package, graph_included=False)
            text = "\n\n".join(
                part for part in (manifest, profile_text, detection_text, limitation) if part
            )

        knowledge_text = self._compose_knowledge(
            package,
            token_budget=min(self.settings.rag_max_context_tokens, max_dynamic_tokens),
        )
        if knowledge_text:
            self.last_inclusion["knowledge"] = (True, None)
            self.last_representation["knowledge"] = (
                "full"
                if package.knowledge and self.last_knowledge_included_count == package.knowledge.included_count
                else "compact"
            )
            trial_manifest = self._compose_provider_manifest(package, graph_included=bool(graph_text))
            trial_text = "\n\n".join(
                part
                for part in (trial_manifest, profile_text, detection_text, graph_text, knowledge_text, limitation)
                if part
            )
            if approx_tokens(trial_text) <= max_dynamic_tokens:
                manifest = trial_manifest
                text = trial_text
            else:
                knowledge_text = ""
                self.last_knowledge_included_count = 0
                self.last_inclusion["knowledge"] = (False, "global_context_limit")
                self.last_representation["knowledge"] = "excluded"
                manifest = self._compose_provider_manifest(package, graph_included=bool(graph_text))
                text = "\n\n".join(
                    part for part in (manifest, profile_text, detection_text, graph_text, limitation) if part
                )

        self.last_parts = {
            "status": manifest,
            "asset_profile": profile_text,
            "detection": detection_text,
            "graph": graph_text,
            "knowledge": knowledge_text,
            "fusion": "",
        }
        self.last_budget["total_dynamic_tokens"] = approx_tokens(text)
        logger.info(
            "event=context_composed_route_aware request_id=%s requested_scope=full_neighbors graph_included=%s graph_representation=%s graph_tokens=%s asset_profile_tokens=%s detection_tokens=%s knowledge_tokens=%s total_dynamic_tokens=%s max_dynamic_tokens=%s required_context_missing=%s",
            request_id,
            graph_included and bool(graph_text),
            self.last_representation.get("graph", "excluded"),
            approx_tokens(graph_text),
            approx_tokens(profile_text),
            approx_tokens(detection_text),
            approx_tokens(knowledge_text),
            approx_tokens(text),
            max_dynamic_tokens,
            self.required_context_missing,
        )
        return text

    def _compact_product_section(
        self,
        result: Any,
        provider_name: str,
        ip: str,
        *,
        token_budget: int,
    ) -> str:
        tag = "ASSET_PROFILE_COMPACT_JSON" if provider_name == "asset_profile" else "ASSET_DETECTION_COMPACT_JSON"
        try:
            source = json.loads(result.serialized_json)
        except (TypeError, ValueError):
            source = result.raw_payload
        priority_keys = (
            "id",
            "ip",
            "ip_address",
            "assetFound",
            "name",
            "hostname",
            "classification",
            "role",
            "confidence",
            "risk_score",
            "os",
            "services",
            "matchedRules",
            "signals",
            "conflicts",
            "metrics",
            "identity",
        )
        compact: dict[str, Any] = {
            "representation": "compact",
            "target_ip": ip,
        }
        if isinstance(source, dict):
            ordered_keys = [key for key in priority_keys if key in source]
            ordered_keys.extend(sorted(key for key in source if key not in ordered_keys))
            included_keys: list[str] = []
            for key in ordered_keys:
                trial = dict(compact)
                trial[key] = self._compact_json_value(source[key])
                section = self._tagged_json(tag, ip, trial)
                if approx_tokens(section) <= token_budget:
                    compact = trial
                    included_keys.append(key)
            compact["source_top_level_key_count"] = len(source)
            compact["included_top_level_key_count"] = len(included_keys)
            compact["omitted_top_level_key_count"] = max(0, len(source) - len(included_keys))
        elif isinstance(source, list):
            compact["items"] = [self._compact_json_value(item) for item in source[:3]]
            compact["source_item_count"] = len(source)
            compact["omitted_item_count"] = max(0, len(source) - 3)
        return self._tagged_json(tag, ip, compact)

    @classmethod
    def _compact_json_value(cls, value: Any, depth: int = 0) -> Any:
        if isinstance(value, str):
            return value if len(value) <= 160 else value[:157] + "..."
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if depth >= 2:
            if isinstance(value, dict):
                return {"summary": "nested_object", "key_count": len(value)}
            if isinstance(value, list):
                return {"summary": "nested_array", "item_count": len(value)}
            return str(value)[:160]
        if isinstance(value, list):
            items = [cls._compact_json_value(item, depth + 1) for item in value[:4]]
            if len(value) > 4:
                items.append({"omitted_item_count": len(value) - 4})
            return items
        if isinstance(value, dict):
            keys = list(value)[:12]
            compact = {key: cls._compact_json_value(value[key], depth + 1) for key in keys}
            if len(value) > len(keys):
                compact["omitted_key_count"] = len(value) - len(keys)
            return compact
        return str(value)[:160]

    @staticmethod
    def _tagged_json(tag: str, ip: str, payload: dict[str, Any]) -> str:
        return "\n".join(
            (
                f'[{tag} ip="{ip}"]',
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                f'[/{tag}]',
            )
        )

    @staticmethod
    def _mark_graph_excluded(package: CopilotContextPackage, reason: str) -> None:
        if not package.graph:
            return
        context = package.graph.context
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

    @staticmethod
    def _compose_json_sections(results: list[Any], tag: str) -> list[tuple[str, str, str]]:
        sections: list[tuple[str, str, str]] = []
        provider_name = "asset_profile" if tag == "ASSET_PROFILE_JSON" else "detection"
        for result in results:
            if result.status not in {"available", "not_found"} or not result.serialized_json:
                continue
            sections.append(
                (
                    provider_name,
                    result.ip,
                    "\n".join((f'[{tag} ip="{result.ip}"]', result.serialized_json, f'[/{tag}]')),
                )
            )
        return sections

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
            entities[item.ip] = {
                "status": item.status,
                "payload_included": included,
                "payload_complete": bool(item.full_payload_fetched and representation == "full"),
                "source_payload_complete": bool(item.full_payload_fetched),
                "representation": representation,
                "stale": bool(item.stale),
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
        if package.graph:
            requested.append("graph")
            context = package.graph.context or {}
            coverage["graph"] = {
                "status": package.graph.status,
                "payload_included": graph_included,
                "requested_scope": context.get("requested_scope", context.get("scope", "none")),
                "candidate_node_count": context.get("candidate_node_count", 0),
                "returned_node_count": context.get("retrieved_node_count", context.get("returned_node_count", 0)),
                "candidate_edge_count": context.get("candidate_edge_count", 0),
                "returned_edge_count": context.get("retrieved_edge_count", context.get("returned_edge_count", 0)),
                "included_node_count": context.get("included_node_count", context.get("context_node_count", 0)),
                "included_edge_count": context.get("included_edge_count", context.get("context_edge_count", 0)),
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
                "model_input_graph_included": graph_included,
                "model_input_graph_complete": bool(
                    graph_included
                    and context.get("serialized_context_complete_for_retrieved_subset", False)
                ),
                "complete_for_user_request": bool(graph_included and context.get("complete_for_user_request", False)),
            }
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
        if not target_entities and package.graph:
            target_entities = [str(item) for item in package.graph.context.get("target_ips", []) if item]
            if not target_entities and package.graph.context.get("target_ip"):
                target_entities = [str(package.graph.context["target_ip"])]
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
