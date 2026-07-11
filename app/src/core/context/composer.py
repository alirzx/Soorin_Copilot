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

    def compose(self, package: CopilotContextPackage, *, request_id: str = "") -> str:
        graph = package.graph
        if not graph or graph.status not in {"available", "not_found"}:
            logger.info(
                "event=context_composer_complete request_id=%s section_count=0 context_chars=0 context_approx_tokens=0",
                request_id,
            )
            return ""

        context = graph.context or {}
        nodes, edges, context_truncated, truncation_reason = self._select_context_records(context)
        context["context_node_count"] = len(nodes)
        context["context_edge_count"] = len(edges)
        context["context_truncated"] = context_truncated
        context["context_truncation_reason"] = truncation_reason
        context["inbound_context_included"] = self._included_peer_count(nodes, graph.target_entity.value if graph.target_entity else "", "inbound")
        context["outbound_context_included"] = self._included_peer_count(nodes, graph.target_entity.value if graph.target_entity else "", "outbound")
        context["bidirectional_context_included"] = min(
            context.get("bidirectional_retrieved", 0) or 0,
            context.get("inbound_context_included", 0) or 0,
            context.get("outbound_context_included", 0) or 0,
        )

        peer_subnets = Counter(str(node.get("subnet") or "other") for node in nodes if node.get("hop") != 0)
        subnet_summary = ", ".join(f"{subnet}{count}" for subnet, count in peer_subnets.most_common(12)) or "none"
        scope = str(context.get("scope", "node_summary"))
        direction = str(context.get("direction", "both"))

        lines = [
            "[SOORIN GRAPH EVIDENCE]",
            "Active UI-selected investigation entity." if graph.target_entity and graph.target_entity.source == "ui" else "Active investigation entity.",
            "Use this section as bounded product evidence. Do not infer beyond it.",
            "Use this evidence only when relevant to the current question.",
            "Describe graph structure as observed communication relationships.",
            "Distinguish observed graph evidence from structural interpretation and unknown evidence.",
            f"Evidence source: {graph.provenance.source if graph.provenance else 'observed_communication_graph'}",
            f"Target IP: {context.get('target_ip') or (graph.target_entity.value if graph.target_entity else '')}",
            f"Node found: {bool(context.get('node_found'))}",
            f"Scope: {context.get('scope', 'node_summary')}, direction={context.get('direction', 'both')}, depth={context.get('depth', 0)}",
        ]

        if graph.status == "available":
            lines.extend(
                [
                    f"Inbound peers: observed_total={context.get('inbound_total', 0)}, graph_retrieved={context.get('inbound_retrieved', context.get('inbound_returned', 0))}, model_context_included={context.get('inbound_context_included', 0)}",
                    f"Outbound peers: observed_total={context.get('outbound_total', 0)}, graph_retrieved={context.get('outbound_retrieved', context.get('outbound_returned', 0))}, model_context_included={context.get('outbound_context_included', 0)}",
                    f"Bidirectional peers: observed_total={context.get('bidirectional_total', 0)}, graph_retrieved={context.get('bidirectional_retrieved', context.get('bidirectional_returned', 0))}, model_context_included={context.get('bidirectional_context_included', 0)}",
                    f"Nodes: candidate={context.get('candidate_node_count', 0)}, graph_retrieved={context.get('retrieved_node_count', context.get('returned_node_count', 0))}, model_context_included={context.get('context_node_count', 0)}",
                    f"Edges: candidate={context.get('candidate_edge_count', 0)}, graph_retrieved={context.get('retrieved_edge_count', context.get('returned_edge_count', 0))}, model_context_included={context.get('context_edge_count', 0)}",
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
                included_peers = self._included_peer_count(nodes, context.get("target_ip", ""), direction)
                if scope == "full_neighbors" and matching_peers <= self.settings.graph_full_enumeration_max_peers and not context_truncated:
                    lines.append(f"All {matching_peers} requested {direction} peers were retrieved and explicitly included.")
                elif scope == "full_neighbors":
                    lines.append(
                        f"{matching_peers} requested {direction} peers were retrieved; {included_peers} are explicitly included in this prompt."
                    )
                    lines.append("The full machine-readable graph retrieval result exists outside the model context.")
                lines.append(f"Included node IDs: {_list_node_ids(nodes, limit=self.settings.graph_context_max_enumerated_nodes)}")
                lines.append(f"Included edges: {_list_edges(edges, limit=self.settings.graph_context_max_enumerated_edges)}")
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

    def _select_context_records(self, context: dict[str, Any]) -> tuple[list[dict[str, object]], list[dict[str, object]], bool, str | None]:
        nodes = list(context.get("nodes") or [])
        edges = list(context.get("edges") or context.get("path_edges") or [])
        scope = str(context.get("scope", "node_summary"))
        direction = str(context.get("direction", "both"))
        peer_count = self._matching_peer_count(context, direction)

        node_limit = self.settings.graph_context_max_enumerated_nodes
        edge_limit = self.settings.graph_context_max_enumerated_edges
        if scope == "full_neighbors" and peer_count <= self.settings.graph_full_enumeration_max_peers:
            node_limit = max(node_limit, len(nodes))
            edge_limit = max(edge_limit, len(edges))
        if scope == "path":
            node_limit = max(node_limit, len(nodes))
            edge_limit = max(edge_limit, len(edges))
        if scope == "node_summary":
            node_limit = min(node_limit, 1)
            edge_limit = 0

        selected_nodes = nodes[:node_limit]
        selected_node_ids = {str(node.get("id")) for node in selected_nodes if node.get("id")}
        selected_edges = [
            edge for edge in edges
            if not selected_node_ids or (str(edge.get("source")) in selected_node_ids and str(edge.get("target")) in selected_node_ids)
        ][:edge_limit]

        text_probe = "\n".join(
            [
                _list_node_ids(selected_nodes, limit=len(selected_nodes) or 1),
                _list_edges(selected_edges, limit=len(selected_edges) or 1),
            ]
        )
        context_token_budget = max(256, min(self.settings.graph_max_context_tokens, self._available_graph_context_tokens()))
        truncated = len(selected_nodes) < len(nodes) or len(selected_edges) < len(edges) or approx_tokens(text_probe) > context_token_budget
        reason = "graph_context_token_budget" if truncated else None
        if len(selected_nodes) < len(nodes):
            reason = "graph_context_node_limit"
        if len(selected_edges) < len(edges):
            reason = "graph_context_edge_limit"
        return selected_nodes, selected_edges, truncated, reason

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
    def _included_peer_count(nodes: list[dict[str, object]], target_ip: object, direction: str) -> int:
        target = str(target_ip or "")
        return len([node for node in nodes if str(node.get("id", "")) != target and node.get("id")])
