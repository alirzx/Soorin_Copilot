"""In-memory conversation store with deterministic compact summaries."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.config.settings import Settings
from src.core.context.models import approx_tokens, compact_preview
from src.core.memory.routing_state import SessionRoutingState


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConversationSnapshot:
    messages: list[dict[str, str]]
    raw_message_count: int
    recent_message_count: int
    summary_present: bool
    summary_tokens_approx: int
    tokens_before_compaction: int
    tokens_after_compaction: int
    summary_updated: bool = False
    summary_error: str = ""


class MemoryStore:
    def __init__(self, max_messages: int) -> None:
        self.max_messages = max(0, max_messages)
        self._history: dict[str, list[dict[str, str]]] = {}
        self._summaries: dict[str, dict[str, Any]] = {}

    def get(self, session_id: str) -> list[dict[str, str]]:
        return list(self._history.get(session_id, []))

    def append(self, session_id: str, role: str, content: str) -> None:
        if self.max_messages == 0:
            return
        history = self._history.setdefault(session_id, [])
        history.append({"role": role, "content": content})
        self._history[session_id] = history[-self.max_messages :]

    @staticmethod
    def fit_messages_to_budget(
        messages: list[dict[str, str]],
        max_tokens: int,
        *,
        prefer_current_evidence: bool = False,
    ) -> list[dict[str, str]]:
        """Return a non-mutating bounded history, dropping old assistant claims first."""
        selected = [dict(message) for message in messages]
        budget = max(0, int(max_tokens))

        def token_count() -> int:
            return sum(approx_tokens(item.get("content", "")) for item in selected)

        if token_count() <= budget:
            return selected
        role_priority = ("assistant", "system", "user") if prefer_current_evidence else ("system", "user", "assistant")
        for role in role_priority:
            index = 0
            while token_count() > budget and index < len(selected):
                if selected[index].get("role") == role:
                    selected.pop(index)
                else:
                    index += 1
        while selected and token_count() > budget:
            selected.pop(0)
        return selected

    def prepare_for_model(
        self,
        session_id: str,
        settings: Settings,
        routing_state: SessionRoutingState,
        *,
        request_id: str = "",
    ) -> ConversationSnapshot:
        updated = False
        error = ""
        before_tokens = self._history_tokens(session_id)
        if settings.conversation_summary_enabled and before_tokens > settings.conversation_summary_trigger_tokens:
            try:
                updated = self.compact_if_needed(session_id, settings, routing_state, request_id=request_id)
            except Exception as exc:
                error = type(exc).__name__
                logger.warning(
                    "event=conversation_summary_failed request_id=%s session_id=%s error_type=%s",
                    request_id,
                    session_id,
                    error,
                )

        summary = self._summaries.get(session_id)
        recent = self.get(session_id)[-settings.conversation_recent_raw_messages :]
        messages: list[dict[str, str]] = []
        if summary:
            messages.append({"role": "system", "content": f"[SOORIN CONVERSATION SUMMARY]\n{summary['text']}"})
        messages.extend(recent)
        after_tokens = sum(approx_tokens(item.get("content", "")) for item in messages)
        return ConversationSnapshot(
            messages=messages,
            raw_message_count=len(self.get(session_id)),
            recent_message_count=len(recent),
            summary_present=bool(summary),
            summary_tokens_approx=approx_tokens(str(summary.get("text", ""))) if summary else 0,
            tokens_before_compaction=before_tokens,
            tokens_after_compaction=after_tokens,
            summary_updated=updated,
            summary_error=error,
        )

    def compact_if_needed(
        self,
        session_id: str,
        settings: Settings,
        routing_state: SessionRoutingState,
        *,
        route: Any | None = None,
        graph_context: dict[str, Any] | None = None,
        request_id: str = "",
    ) -> bool:
        history = self.get(session_id)
        if not settings.conversation_summary_enabled or not history:
            return False
        tokens = sum(approx_tokens(item.get("content", "")) for item in history)
        if tokens <= settings.conversation_summary_trigger_tokens:
            return False
        existing = self._summaries.get(session_id)
        if existing and int(existing.get("summary_source_message_count", 0)) >= len(history):
            return False

        recent_count = max(1, settings.conversation_recent_raw_messages)
        older = history[:-recent_count]
        recent = history[-recent_count:]
        summary_payload = self._build_summary_payload(older, routing_state, route=route, graph_context=graph_context)
        text = json.dumps(summary_payload, sort_keys=True)
        max_chars = max(200, settings.conversation_summary_max_tokens * 4)
        if len(text) > max_chars:
            text = text[: max_chars - 20] + "...[truncated]"
        self._summaries[session_id] = {
            "text": text,
            "summary_updated_at": datetime.now(timezone.utc).isoformat(),
            "summary_source_message_count": len(history),
        }
        self._history[session_id] = recent
        logger.info(
            "event=conversation_summary_updated request_id=%s session_id=%s source_messages=%s recent_messages=%s summary_tokens=%s tokens_before=%s",
            request_id,
            session_id,
            len(history),
            len(recent),
            approx_tokens(text),
            tokens,
        )
        return True

    def _history_tokens(self, session_id: str) -> int:
        return sum(approx_tokens(item.get("content", "")) for item in self.get(session_id))

    @staticmethod
    def _build_summary_payload(
        older: list[dict[str, str]],
        routing_state: SessionRoutingState,
        *,
        route: Any | None,
        graph_context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        older_user_messages = [
            compact_preview(item.get("content", ""), limit=140)
            for item in older
            if item.get("role") == "user"
        ][-5:]
        known_findings: list[str] = []
        if graph_context:
            if graph_context.get("relationship_mode") == "direct":
                known_findings.append(
                    "Direct relationship checked: "
                    f"forward={bool(graph_context.get('forward_edge'))}, reverse={bool(graph_context.get('reverse_edge'))}."
                )
            if graph_context.get("relationship_mode") == "compare":
                known_findings.append(
                    "Comparison checked: "
                    f"shared_peer_total={graph_context.get('shared_peer_total', 0)}, "
                    f"entity_a_unique={graph_context.get('entity_a_unique_peer_total', 0)}, "
                    f"entity_b_unique={graph_context.get('entity_b_unique_peer_total', 0)}."
                )
            target_values = [
                str(item)
                for item in (graph_context.get("target_ips") or ([graph_context.get("target_ip")] if graph_context.get("target_ip") else []))
                if item
            ]
            if target_values:
                known_findings.append(
                    "Graph context retrieved for "
                    f"{', '.join(target_values)} with scope={graph_context.get('scope')}."
                )
        previous_route = {}
        if route:
            previous_route = {
                "intent": getattr(route, "intent", None),
                "scope": getattr(route, "scope", None),
                "direction": getattr(route, "direction", None),
                "depth": getattr(route, "depth", None),
            }
        return {
            "active_entities": list(routing_state.active_entities) or ([routing_state.active_ip] if routing_state.active_ip else []),
            "investigation_topic": previous_route.get("intent") or routing_state.previous_intent or "",
            "known_findings": known_findings[:5],
            "previous_routes": [previous_route] if previous_route else [],
            "older_user_requests": older_user_messages,
            "unresolved_questions": [],
            "user_constraints": [],
        }
