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


class ContextComposer:
    """Turn typed provider results into bounded model evidence without lossy product transforms."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.last_parts: dict[str, str] = {
            "status": "",
            "asset_profile": "",
            "detection": "",
            "graph": "",
            "fusion": "",
        }
        self.last_inclusion: dict[str, tuple[bool, str | None]] = {}

    def compose(self, package: CopilotContextPackage, *, request_id: str = "", base_input_tokens: int = 0) -> str:
        profile_sections = self._compose_json_sections(package.asset_profiles, "ASSET_PROFILE_JSON")
        detection_sections = self._compose_json_sections(package.detections, "ASSET_DETECTION_JSON")
        graph_candidate = self._compose_graph(package, request_id=request_id)
        max_dynamic_tokens = max(
            0,
            self.settings.llm_context_window_tokens
            - self.settings.llm_reserved_output_tokens
            - self.settings.llm_context_safety_margin_tokens
            - base_input_tokens,
        )

        self.last_inclusion = {}
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
                (included_profiles if provider_name == "asset_profile" else included_detections).append(section)
            else:
                self.last_inclusion[key] = (False, "global_context_limit")
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

        manifest = self._compose_provider_manifest(package, graph_included=graph_included)
        profile_text = "\n\n".join(included_profiles)
        detection_text = "\n\n".join(included_detections)
        parts = [part for part in (manifest, profile_text, detection_text, graph_text) if part]
        text = "\n\n".join(parts)
        self.last_parts = {
            "status": manifest,
            "asset_profile": profile_text,
            "detection": detection_text,
            "graph": graph_text,
            "fusion": "",
        }
        logger.info(
            "event=context_composed request_id=%s providers=%s manifest_chars=%s asset_profile_chars=%s detection_chars=%s graph_chars=%s total_dynamic_chars=%s total_dynamic_approx_tokens=%s max_dynamic_tokens=%s",
            request_id,
            ",".join(name for name, value in (("manifest", manifest), ("asset_profile", profile_text), ("detection", detection_text), ("graph", graph_text)) if value),
            len(manifest),
            len(profile_text),
            len(detection_text),
            len(graph_text),
            len(text),
            approx_tokens(text),
            max_dynamic_tokens,
        )
        return text

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
            included = self.last_inclusion.get(f"{provider_name}:{item.ip}", (False, None))[0]
            entities[item.ip] = {
                "status": item.status,
                "payload_included": included,
                "payload_complete": bool(item.full_payload_fetched),
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
                "retrieval_complete": context.get("retrieval_complete", False),
                "retrieval_truncated": context.get("retrieval_truncated", False),
                "retrieval_truncation_reason": context.get("retrieval_truncation_reason"),
                "serialized_context_complete_for_retrieved_subset": context.get("serialized_context_complete_for_retrieved_subset", False),
                "serialized_context_truncated": context.get("serialized_context_truncated", False),
                "serialized_context_truncation_reason": context.get("serialized_context_truncation_reason"),
                "requested_scope_complete": context.get("requested_scope_complete", False),
                "complete_for_user_request": bool(graph_included and context.get("complete_for_user_request", False)),
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

    def _compose_graph(self, package: CopilotContextPackage, *, request_id: str = "") -> str:
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
