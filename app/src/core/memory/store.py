"""In-memory conversation store with deterministic compact summaries."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.config.settings import Settings
from src.core.context.models import approx_tokens, compact_preview
from src.core.memory.episodes import (
    EpisodeRecord,
    InMemoryMemoryRepository,
    MemoryContextKey,
    MemoryRepository,
    WorkingMemory,
)
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
    context_identity: str = ""
    episode_transition: bool = False
    previous_episode_summary_included: bool = False
    episode_count: int = 0


class MemoryStore:
    def __init__(self, max_messages: int, repository: MemoryRepository | None = None) -> None:
        self.max_messages = max(0, max_messages)
        self._history: dict[str, list[dict[str, str]]] = {}
        self._latest_completed_turns: dict[str, list[dict[str, str]]] = {}
        self._summaries: dict[str, dict[str, Any]] = {}
        self.repository = repository or InMemoryMemoryRepository()

    def get(self, session_id: str) -> list[dict[str, str]]:
        return list(self._history.get(session_id, []))

    def append(self, session_id: str, role: str, content: str) -> None:
        if self.max_messages == 0:
            return
        history = self._history.setdefault(session_id, [])
        history.append({"role": role, "content": content})
        self._history[session_id] = history[-self.max_messages :]

    def record_turn(
        self,
        session_id: str,
        user_content: str,
        assistant_content: str,
        context_key: MemoryContextKey,
        *,
        providers: tuple[str, ...] = (),
        limitations: tuple[str, ...] = (),
        scope: str = "none",
    ) -> None:
        """Record one bounded turn without storing provider payloads or prompts."""
        if self.max_messages == 0:
            return
        working = self.repository.get_working(session_id)
        if working is None or working.context_key != context_key:
            working = WorkingMemory(
                session_id=session_id,
                context_key=context_key,
                episode_id=EpisodeRecord.create(session_id, context_key).episode_id,
            )
        self.append(session_id, "user", user_content)
        self.append(session_id, "assistant", assistant_content)
        self._latest_completed_turns[session_id] = [
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": assistant_content},
        ]
        working.latest_user_turn = compact_preview(user_content, limit=400)
        working.latest_assistant_turn = compact_preview(assistant_content, limit=600)
        working.last_providers = tuple(dict.fromkeys(providers))
        working.last_scope = scope
        working.limitations = tuple(dict.fromkeys(compact_preview(item, limit=200) for item in limitations))[:8]
        self.repository.set_working(working)

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
        context_key: MemoryContextKey | None = None,
        request_id: str = "",
    ) -> ConversationSnapshot:
        transitioned = False
        previous_episode_summary_included = False
        if context_key is not None:
            transitioned, previous_episode_summary_included = self._activate_context(
                session_id,
                context_key,
                settings,
                request_id=request_id,
            )
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
        recent = self.get(session_id)[-max(2, settings.conversation_recent_raw_messages) :]
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
            context_identity=context_key.identity if context_key else "legacy",
            episode_transition=transitioned,
            previous_episode_summary_included=previous_episode_summary_included,
            episode_count=len(self.repository.list_episodes(session_id)),
        )

    @staticmethod
    def _latest_completed_turn(history: list[dict[str, str]]) -> list[dict[str, str]]:
        """Return the newest adjacent user/assistant pair, if one exists."""
        for index in range(len(history) - 2, -1, -1):
            if (
                history[index].get("role") == "user"
                and history[index + 1].get("role") == "assistant"
            ):
                return [dict(history[index]), dict(history[index + 1])]
        return []

    def recent_for_routing(self, session_id: str, limit: int) -> list[dict[str, str]]:
        """Return bounded raw continuity for entity/reference resolution only."""
        recent = self.get(session_id)[-max(0, limit) :]
        latest = self._latest_completed_turns.get(session_id, [])
        if latest:
            keys = {(item.get("role"), item.get("content")) for item in recent}
            for item in latest:
                key = (item.get("role"), item.get("content"))
                if key not in keys:
                    recent.append(dict(item))
                    keys.add(key)
        return recent[-max(2, limit) :]

    def _activate_context(
        self,
        session_id: str,
        context_key: MemoryContextKey,
        settings: Settings,
        *,
        request_id: str,
    ) -> tuple[bool, bool]:
        working = self.repository.get_working(session_id)
        if working is None:
            episode = EpisodeRecord.create(session_id, context_key)
            self.repository.set_working(
                WorkingMemory(session_id=session_id, context_key=context_key, episode_id=episode.episode_id)
            )
            return False, False
        if working.context_key == context_key:
            return False, False

        latest_completed = self._latest_completed_turn(self.get(session_id))
        if latest_completed:
            self._latest_completed_turns[session_id] = latest_completed
        self._archive_current_episode(session_id, working, settings)
        self._history[session_id] = []
        self._summaries.pop(session_id, None)
        matching = next(
            (
                episode
                for episode in reversed(self.repository.list_episodes(session_id))
                if episode.context_key == context_key and episode.compact_summary
            ),
            None,
        )
        if matching:
            self._summaries[session_id] = {
                "text": matching.compact_summary,
                "summary_updated_at": matching.updated_at,
                "summary_source_message_count": 0,
            }
        episode = EpisodeRecord.create(session_id, context_key)
        self.repository.set_working(
            WorkingMemory(
                session_id=session_id,
                context_key=context_key,
                episode_id=episode.episode_id,
                compact_summary=matching.compact_summary if matching else "",
            )
        )
        logger.info(
            "event=memory_episode_transition request_id=%s session_id=%s context_identity=%s previous_summary_reused=%s",
            request_id,
            session_id,
            context_key.identity,
            bool(matching),
        )
        return True, bool(matching)

    def _archive_current_episode(
        self,
        session_id: str,
        working: WorkingMemory,
        settings: Settings,
    ) -> None:
        history = self.get(session_id)
        summary = self._summaries.get(session_id, {}).get("text", "")
        if history:
            payload = {
                "context": working.context_key.identity,
                "recent_user_requests": [
                    compact_preview(item.get("content", ""), limit=160)
                    for item in history
                    if item.get("role") == "user"
                ][-3:],
                "last_assistant_summary": next(
                    (
                        compact_preview(item.get("content", ""), limit=280)
                        for item in reversed(history)
                        if item.get("role") == "assistant"
                    ),
                    "",
                ),
            }
            summary = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        max_chars = max(200, settings.conversation_summary_max_tokens * 4)
        if len(summary) > max_chars:
            summary = summary[: max_chars - 20] + "...[truncated]"
        self.repository.add_episode(
            EpisodeRecord(
                episode_id=working.episode_id,
                session_id=session_id,
                context_key=working.context_key,
                compact_summary=summary,
                limitations=working.limitations,
                last_providers=working.last_providers,
                last_scope=working.last_scope,
                turn_count=sum(1 for item in history if item.get("role") == "user"),
            )
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

        recent_count = max(2, settings.conversation_recent_raw_messages)
        older = history[:-recent_count]
        recent = history[-recent_count:]
        latest_completed = self._latest_completed_turn(history)
        if latest_completed:
            recent_keys = {(item.get("role"), item.get("content")) for item in recent}
            for item in latest_completed:
                key = (item.get("role"), item.get("content"))
                if key not in recent_keys:
                    recent.append(item)
                    recent_keys.add(key)
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
        working = self.repository.get_working(session_id)
        if working is not None:
            working.compact_summary = text
            self.repository.set_working(working)
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
