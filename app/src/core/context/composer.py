"""Compose bounded model-facing context from typed provider results."""

from __future__ import annotations

import logging

from src.core.context.models import CopilotContextPackage, approx_tokens


logger = logging.getLogger(__name__)


def _list_values(values: list[str], limit: int = 10) -> str:
    if not values:
        return "none"
    shown = values[:limit]
    suffix = f" (+{len(values) - limit} more)" if len(values) > limit else ""
    return ", ".join(shown) + suffix


class ContextComposer:
    """Turn structured context into a bounded instruction/evidence message."""

    def compose(self, package: CopilotContextPackage, *, request_id: str = "") -> str:
        graph = package.graph
        if not graph or graph.status not in {"available", "not_found"}:
            logger.info(
                "event=context_composer_complete request_id=%s section_count=0 context_chars=0 context_approx_tokens=0",
                request_id,
            )
            return ""

        context = graph.context or {}
        degree = context.get("degree") or {}
        lines = [
            "[SOORIN GRAPH EVIDENCE]",
            "Use this section as bounded product evidence. Do not infer beyond it.",
            "Describe graph structure as observed communication relationships.",
            f"Evidence source: {graph.provenance.source if graph.provenance else 'observed_communication_graph'}",
            f"Target IP: {context.get('target_ip') or (graph.target_entity.value if graph.target_entity else '')}",
            f"Node found: {bool(context.get('node_found'))}",
        ]

        if graph.status == "available":
            lines.extend(
                [
                    f"Degree: inbound={degree.get('in', 0)}, outbound={degree.get('out', 0)}, total={degree.get('total', 0)}",
                    f"Top inbound peers: {_list_values(list(context.get('top_inbound_peers') or []))}",
                    f"Top outbound peers: {_list_values(list(context.get('top_outbound_peers') or []))}",
                    f"Bidirectional peers: {_list_values(list(context.get('bidirectional_peers') or []))}",
                    f"Outbound subnets reached: {_list_values(list(context.get('subnets_reached') or []), limit=12)}",
                ]
            )
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
            ]
        )

        text = "\n".join(lines)
        logger.info(
            "event=context_composer_complete request_id=%s section_count=1 context_chars=%s context_approx_tokens=%s graph_status=%s limitation_count=%s",
            request_id,
            len(text),
            approx_tokens(text),
            graph.status,
            len(limitations),
        )
        return text
