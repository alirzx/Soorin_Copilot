"""Compose bounded model-facing context from typed provider results."""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from src.config.settings import Settings, get_settings
from src.core.context.models import CopilotContextPackage, approx_tokens


logger = logging.getLogger(__name__)


def _list_values(values: list[str], limit: int = 10) -> str:
    if not values:
        return "none"
    shown = values[:limit]
    suffix = f" (+{len(values) - limit} more)" if len(values) > limit else ""
    return ", ".join(shown) + suffix


def _list_node_ids(nodes: list[dict[str, object]], limit: int = 15) -> str:
    values = [str(node.get("id", "")) for node in nodes if node.get("id")]
    return _list_values(values, limit=limit)


def _list_edges(edges: list[dict[str, object]], limit: int = 15) -> str:
    values = [f"{edge.get('source')} -> {edge.get('target')}" for edge in edges]
    return _list_values(values, limit=limit)


class ContextComposer:
    """Turn structured context into a bounded instruction/evidence message."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.last_parts: dict[str, str] = {"graph": "", "detection": "", "fusion": ""}

    def compose(self, package: CopilotContextPackage, *, request_id: str = "") -> str:
        detection = package.detection
        detection_text = (
            detection.rendered_context
            if detection and detection.status in {"available", "not_found"} and detection.rendered_context
            else ""
        )
        graph_text = self._compose_graph(package, request_id=request_id)
        fusion_text = self._compose_alignment(package)
        self.last_parts = {"graph": graph_text, "detection": detection_text, "fusion": fusion_text}
        parts = [part for part in [detection_text, graph_text, fusion_text] if part]
        text = "\n\n".join(parts)
        logger.info(
            "event=context_composed request_id=%s providers=%s graph_chars=%s detection_chars=%s fusion_chars=%s total_dynamic_chars=%s total_dynamic_approx_tokens=%s",
            request_id,
            ",".join(
                name
                for name, part in [
                    ("graph", graph_text),
                    ("detection", detection_text),
                    ("fusion", fusion_text),
                ]
                if part
            )
            or "none",
            len(graph_text),
            len(detection_text),
            len(fusion_text),
            len(text),
            approx_tokens(text),
        )
        return text

    def _compose_graph(self, package: CopilotContextPackage, *, request_id: str = "") -> str:
        graph = package.graph
        if not graph or graph.status not in {"available", "not_found"}:
            logger.info(
                "event=context_composer_complete request_id=%s section_count=0 context_chars=0 context_approx_tokens=0",
                request_id,
            )
            return ""

        context = graph.context or {}
        nodes, edges, truncation_reasons = self._select_context_records(context)
        context_truncated = bool(truncation_reasons)
        truncation_reason = truncation_reasons[0] if truncation_reasons else None
        retrieved_nodes = list(context.get("nodes") or [])
        retrieved_edges = list(context.get("edges") or context.get("path_edges") or [])
        retrieved_node_count = int(context.get("retrieved_node_count", len(retrieved_nodes)) or 0)
        retrieved_edge_count = int(context.get("retrieved_edge_count", len(retrieved_edges)) or 0)
        context["candidate_node_count"] = max(
            int(context.get("candidate_node_count", retrieved_node_count) or 0),
            retrieved_node_count,
        )
        context["candidate_edge_count"] = max(
            int(context.get("candidate_edge_count", retrieved_edge_count) or 0),
            retrieved_edge_count,
        )
        context["retrieved_node_count"] = retrieved_node_count
        context["retrieved_edge_count"] = retrieved_edge_count
        context["included_node_count"] = len(nodes)
        context["included_edge_count"] = len(edges)
        context["context_node_count"] = len(nodes)
        context["context_edge_count"] = len(edges)
        context["context_truncated"] = context_truncated
        context["context_truncation_reasons"] = truncation_reasons
        context["context_truncation_reason"] = truncation_reason
        context["context_mode"] = "aggregate_only" if context.get("scope") == "node_summary" else "enumerated"
        context["aggregate_only_context"] = context.get("scope") == "node_summary"
        direction_counts = self._included_direction_counts(nodes, context.get("target_ip", ""))
        context["inbound_context_included"] = direction_counts["inbound"]
        context["outbound_context_included"] = direction_counts["outbound"]
        context["bidirectional_context_included"] = direction_counts["bidirectional"]

        peer_subnets = Counter(str(node.get("subnet") or "other") for node in nodes if node.get("hop") != 0)
        subnet_summary = ", ".join(f"{subnet}{count}" for subnet, count in peer_subnets.most_common(12)) or "none"
        scope = str(context.get("scope", "node_summary"))
        direction = str(context.get("direction", "both"))
        relationship_mode = str(context.get("relationship_mode", "none"))
        target_ips = [str(item) for item in (context.get("target_ips") or []) if item]

        lines = [
            "[SOORIN GRAPH EVIDENCE]",
            "Active UI-selected investigation entity." if graph.target_entity and graph.target_entity.source == "ui" else "Active investigation entity.",
            "Use this section as bounded product evidence. Do not infer beyond it.",
            "Use this evidence only when relevant to the current question.",
            "Describe graph structure as observed communication relationships.",
            "Distinguish observed graph evidence from structural interpretation and unknown evidence.",
            f"Evidence source: {graph.provenance.source if graph.provenance else 'observed_communication_graph'}",
            f"Target IPs: {_list_values(target_ips, limit=2)}"
            if target_ips
            else f"Target IP: {context.get('target_ip') or (graph.target_entity.value if graph.target_entity else '')}",
            f"Node found: {bool(context.get('node_found'))}",
            f"Scope: {context.get('scope', 'node_summary')}, direction={context.get('direction', 'both')}, depth={context.get('depth', 0)}",
        ]

        if graph.status == "available":
            if relationship_mode == "direct":
                lines.extend(
                    [
                        f"Relationship source: {context.get('source', '')}; present={context.get('source_present', False)}",
                        f"Relationship target: {context.get('target', '')}; present={context.get('target_present', False)}",
                        f"Forward edge source_to_target: {context.get('forward_edge', False)}",
                        f"Reverse edge target_to_source: {context.get('reverse_edge', False)}",
                        f"Direct relationship: {context.get('relationship_status', context.get('relationship', 'unknown'))}; bidirectional={context.get('bidirectional', False)}",
                    ]
                )
            if relationship_mode == "compare":
                entity_a = dict(context.get("entity_a") or {})
                entity_b = dict(context.get("entity_b") or {})
                direct = dict(context.get("direct_relationship") or {})
                degree_comparison = dict(context.get("degree_comparison") or {})
                subnet_comparison = dict(context.get("subnet_comparison") or {})
                lines.extend(
                    [
                        f"Comparison entities: {_list_values([str(item) for item in context.get('entities', [])], limit=2)}",
                        f"Entity A summary: ip={entity_a.get('ip', '')}, present={entity_a.get('present', False)}, inbound_total={entity_a.get('inbound_total', 0)}, outbound_total={entity_a.get('outbound_total', 0)}, bidirectional_total={entity_a.get('bidirectional_total', 0)}",
                        f"Entity B summary: ip={entity_b.get('ip', '')}, present={entity_b.get('present', False)}, inbound_total={entity_b.get('inbound_total', 0)}, outbound_total={entity_b.get('outbound_total', 0)}, bidirectional_total={entity_b.get('bidirectional_total', 0)}",
                        f"Direct relationship A->B={direct.get('a_to_b', False)}, B->A={direct.get('b_to_a', False)}, relationship={direct.get('relationship_status', direct.get('relationship', 'unknown'))}",
                        f"Degree comparison: entity_a_total_peers={degree_comparison.get('entity_a_total_peer_count', 0)}, entity_b_total_peers={degree_comparison.get('entity_b_total_peer_count', 0)}, broader_outbound={degree_comparison.get('broader_outbound_entity', 'unknown')}, broader_inbound={degree_comparison.get('broader_inbound_entity', 'unknown')}",
                        f"Subnet comparison: shared={_list_values([str(item) for item in subnet_comparison.get('shared_subnets', [])], limit=8)}, entity_a_unique={_list_values([str(item) for item in subnet_comparison.get('entity_a_unique_subnets', [])], limit=8)}, entity_b_unique={_list_values([str(item) for item in subnet_comparison.get('entity_b_unique_subnets', [])], limit=8)}",
                        f"Shared peers: total={context.get('shared_peer_total', 0)}, retrieved={context.get('shared_peers_retrieved_count', 0)}, values={_list_values([str(item) for item in context.get('shared_peers_retrieved', [])], limit=self.settings.graph_comparison_max_shared_peers)}",
                        f"Unique peer totals: entity_a={context.get('entity_a_unique_peer_total', 0)}, entity_b={context.get('entity_b_unique_peer_total', 0)}",
                    ]
                )
            lines.extend(
                [
                    f"Inbound peers: observed_total={context.get('inbound_total', 0)}, graph_retrieved={context.get('inbound_retrieved', context.get('inbound_returned', 0))}, model_context_included={context.get('inbound_context_included', 0)}",
                    f"Outbound peers: observed_total={context.get('outbound_total', 0)}, graph_retrieved={context.get('outbound_retrieved', context.get('outbound_returned', 0))}, model_context_included={context.get('outbound_context_included', 0)}",
                    f"Bidirectional peers: observed_total={context.get('bidirectional_total', 0)}, graph_retrieved={context.get('bidirectional_retrieved', context.get('bidirectional_returned', 0))}, model_context_included={context.get('bidirectional_context_included', 0)}",
                    f"Nodes: candidate={context.get('candidate_node_count', 0)}, graph_retrieved={context.get('retrieved_node_count', context.get('returned_node_count', 0))}, model_context_included={context.get('context_node_count', 0)}",
                    f"Edges: candidate={context.get('candidate_edge_count', 0)}, graph_retrieved={context.get('retrieved_edge_count', context.get('returned_edge_count', 0))}, model_context_included={context.get('context_edge_count', 0)}",
                    f"Context mode: {context.get('context_mode', 'enumerated')}",
                    f"Peer subnet distribution in this prompt: {subnet_summary}",
                    f"Outbound subnets reached: {_list_values(list(context.get('subnets_reached') or []), limit=12)}",
                ]
            )
            if scope == "node_summary":
                lines.append("Neighbor lists are intentionally not enumerated for node_summary.")
            elif scope == "two_hop":
                hop_counts = Counter(int(node.get("hop", 0)) for node in nodes)
                lines.append(
                    f"Hop summary in this prompt: hop0={hop_counts.get(0, 0)}, hop1={hop_counts.get(1, 0)}, hop2={hop_counts.get(2, 0)}"
                )
                lines.append(f"Representative node IDs: {_list_node_ids(nodes, limit=30)}")
                lines.append(f"Representative edges: {_list_edges(edges, limit=30)}")
            elif scope == "path":
                lines.append(f"Path nodes: {_list_values(list(context.get('path_nodes') or []), limit=40)}")
                lines.append(f"Path edges: {_list_edges(edges, limit=40)}")
            else:
                matching_peers = self._matching_peer_count(context, direction)
                included_peers = direction_counts.get(direction, direction_counts["union"])
                if scope == "full_neighbors" and matching_peers <= self.settings.graph_full_enumeration_max_peers and not context_truncated:
                    lines.append(f"All {matching_peers} requested {direction} peers were retrieved and explicitly included.")
                elif scope == "full_neighbors":
                    lines.append(
                        f"{matching_peers} requested {direction} peers were retrieved; {included_peers} are explicitly included in this prompt."
                    )
                    lines.append("The full machine-readable graph retrieval result exists outside the model context.")
                lines.append(f"Included node IDs: {_list_node_ids(nodes, limit=self.settings.graph_context_max_enumerated_nodes)}")
                lines.append(f"Included edges: {_list_edges(edges, limit=self.settings.graph_context_max_enumerated_edges)}")
            if context.get("formal_anomaly_evidence_available") is False:
                lines.append("Formal anomaly evidence: unavailable; no dedicated anomaly provider was used.")
                lines.append("Available analysis: bounded structural interpretation of the supplied graph evidence only.")
            if context.get("path_exists") is not None:
                lines.append(f"Path exists: {bool(context.get('path_exists'))}; hop_count={context.get('hop_count')}")
            if context.get("retrieval_truncated"):
                lines.append(f"Graph retrieval truncation: true; reason={context.get('retrieval_truncation_reason') or 'unknown'}")
            else:
                lines.append("Graph retrieval truncation: false.")
            if context.get("context_truncated"):
                lines.append(f"Model-context truncation: true; reason={context.get('context_truncation_reason') or 'graph_context_token_budget'}")
            else:
                lines.append("Model-context truncation: false.")
        else:
            lines.append("No node for this IP exists in the currently loaded observed communication graph.")

        limitations = list(context.get("limitations") or graph.limitations or [])
        if limitations:
            lines.append("Limitations:")
            lines.extend(f"- {item}" for item in limitations[:5])
        lines.extend(
            [
                "Grounding rules:",
                "- Mention graph evidence only when useful for the user's question.",
                "- If the evidence is missing or insufficient, say what is missing.",
                "- Do not claim live logs, live assets, routing proof, or raw topology access.",
                "- Inbound-only can be called inbound-only or sink-like in this graph, not server/client/asset role proof.",
                "- Do not describe edges as successful sessions or established connections unless supplied.",
                "- Do not infer ports, protocols, bytes, traffic volume, processes, maliciousness, or physical topology.",
                "- Prefer 'high-degree' or 'highly connected' over 'highly active' unless temporal traffic evidence is supplied.",
            ]
        )

        text = "\n".join(lines)
        logger.info(
            "event=context_composer_complete request_id=%s section_count=1 context_chars=%s context_approx_tokens=%s graph_status=%s limitation_count=%s retrieval_nodes=%s context_nodes=%s retrieval_edges=%s context_edges=%s context_truncated=%s",
            request_id,
            len(text),
            approx_tokens(text),
            graph.status,
            len(limitations),
            context.get("retrieved_node_count", context.get("returned_node_count", 0)),
            context.get("context_node_count", 0),
            context.get("retrieved_edge_count", context.get("returned_edge_count", 0)),
            context.get("context_edge_count", 0),
            context.get("context_truncated", False),
        )
        return text

    @staticmethod
    def _compose_alignment(package: CopilotContextPackage) -> str:
        graph = package.graph
        detection = package.detection
        if not graph or not detection or graph.status != "available" or detection.status != "available" or not detection.evidence:
            return ""

        context = graph.context or {}
        evidence = detection.evidence
        role_text = " ".join(
            str(item or "")
            for item in [
                evidence.classification.primary_role,
                evidence.classification.inferred_device_type,
                evidence.tagging.stored_tag,
                evidence.tagging.stored_sub_tag,
                evidence.tagging.tag,
                evidence.tagging.sub_tag,
            ]
        ).lower()
        inbound_total = int(context.get("inbound_total", 0) or 0)
        outbound_total = int(context.get("outbound_total", 0) or 0)
        agreement: list[str] = []
        conflicts: list[str] = []
        unknowns: list[str] = []

        if "workstation" in role_text and outbound_total > inbound_total:
            agreement.append("Detected workstation role and outbound-heavy graph behavior may align.")
        if "workstation" in role_text and inbound_total > max(1, outbound_total * 2):
            conflicts.append("Detected workstation role may conflict with heavy server-like inbound exposure.")
        if "network" in role_text and not any(
            key in evidence.signals.metrics
            for key in ["snmp", "snmp_seen", "network_os", "network_device_evidence"]
        ):
            conflicts.append("Detected network-device wording has weak or missing SNMP/network-OS evidence.")
        if not agreement:
            unknowns.append("No deterministic agreement signal was found; this is not negative proof.")
        if not conflicts:
            unknowns.append("No deterministic conflict signal was found in the bounded evidence.")

        lines = [
            "[SOORIN EVIDENCE ALIGNMENT]",
            "agreement:",
            *[f"- {item}" for item in agreement],
            "conflicts:",
            *[f"- {item}" for item in conflicts],
            "unknowns:",
            *[f"- {item}" for item in unknowns],
        ]
        return "\n".join(lines)

    def _select_context_records(
        self,
        context: dict[str, Any],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str]]:
        nodes = list(context.get("nodes") or [])
        edges = list(context.get("edges") or context.get("path_edges") or [])
        scope = str(context.get("scope", "node_summary"))
        direction = str(context.get("direction", "both"))
        peer_count = self._matching_peer_count(context, direction)
        target_ip = str(context.get("target_ip") or "")

        if scope == "node_summary":
            selected_nodes = [node for node in nodes if str(node.get("id") or "") == target_ip][:1]
            return selected_nodes, [], []

        node_limit = self.settings.graph_context_max_enumerated_nodes
        edge_limit = self.settings.graph_context_max_enumerated_edges
        if scope == "full_neighbors" and peer_count <= self.settings.graph_full_enumeration_max_peers:
            node_limit = max(node_limit, len(nodes))
            edge_limit = max(edge_limit, len(edges))
        if scope == "path":
            node_limit = max(node_limit, len(nodes))
            edge_limit = max(edge_limit, len(edges))
        ordered_nodes = nodes
        if target_ip:
            ordered_nodes = [
                *[node for node in nodes if str(node.get("id") or "") == target_ip],
                *[node for node in nodes if str(node.get("id") or "") != target_ip],
            ]
        selected_nodes = ordered_nodes[:node_limit]
        selected_node_ids = {str(node.get("id")) for node in selected_nodes if node.get("id")}
        eligible_edges = [
            edge for edge in edges
            if selected_node_ids
            and str(edge.get("source")) in selected_node_ids
            and str(edge.get("target")) in selected_node_ids
        ]
        selected_edges = eligible_edges[:edge_limit]
        truncation_reasons: list[str] = []
        if len(selected_nodes) < len(nodes):
            truncation_reasons.append("graph_context_node_limit")
        if len(selected_edges) < len(eligible_edges):
            truncation_reasons.append("graph_context_edge_limit")

        def probe_tokens() -> int:
            text_probe = "\n".join([
                _list_node_ids(selected_nodes, limit=len(selected_nodes) or 1),
                _list_edges(selected_edges, limit=len(selected_edges) or 1),
            ])
            return approx_tokens(text_probe)

        context_token_budget = max(256, min(self.settings.graph_max_context_tokens, self._available_graph_context_tokens()))
        token_records_removed = False
        while selected_edges and probe_tokens() > context_token_budget:
            selected_edges.pop()
            token_records_removed = True
        while len(selected_nodes) > 1 and probe_tokens() > context_token_budget:
            selected_nodes.pop()
            selected_node_ids = {str(node.get("id")) for node in selected_nodes if node.get("id")}
            selected_edges = [
                edge
                for edge in selected_edges
                if str(edge.get("source")) in selected_node_ids and str(edge.get("target")) in selected_node_ids
            ]
            token_records_removed = True
        if token_records_removed:
            truncation_reasons.append("graph_context_token_budget")
        return selected_nodes, selected_edges, truncation_reasons

    def _available_graph_context_tokens(self) -> int:
        return (
            self.settings.llm_context_window_tokens
            - self.settings.llm_reserved_output_tokens
            - self.settings.llm_context_safety_margin_tokens
        )

    @staticmethod
    def _matching_peer_count(context: dict[str, Any], direction: str) -> int:
        if direction == "inbound":
            return int(context.get("inbound_retrieved", context.get("inbound_returned", 0)) or 0)
        if direction == "outbound":
            return int(context.get("outbound_retrieved", context.get("outbound_returned", 0)) or 0)
        return max(
            int(context.get("inbound_retrieved", context.get("inbound_returned", 0)) or 0),
            int(context.get("outbound_retrieved", context.get("outbound_returned", 0)) or 0),
            int(context.get("bidirectional_retrieved", context.get("bidirectional_returned", 0)) or 0),
        )

    @staticmethod
    def _included_direction_counts(nodes: list[dict[str, object]], target_ip: object) -> dict[str, int]:
        target = str(target_ip or "")
        inbound = {
            str(node.get("id"))
            for node in nodes
            if node.get("id") and str(node.get("id")) != target and bool(node.get("inbound"))
        }
        outbound = {
            str(node.get("id"))
            for node in nodes
            if node.get("id") and str(node.get("id")) != target and bool(node.get("outbound"))
        }
        bidirectional = inbound.intersection(outbound)
        return {
            "inbound": len(inbound),
            "outbound": len(outbound),
            "bidirectional": len(bidirectional),
            "both": len(inbound.union(outbound)),
            "union": len(inbound.union(outbound)),
        }
