"""In-memory conversation store with deterministic compact summaries."""

from __future__ import annotations

import json
import hashlib
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from src.config.settings import Settings
from src.core.context.models import approx_tokens, compact_preview
from src.core.memory.baselines import (
    MAX_BASELINE_PROJECTIONS,
    MAX_BASELINE_PROJECTION_BYTES,
    MAX_BASELINE_TOTAL_BYTES,
    InvestigationBaseline,
)
from src.core.memory.episodes import (
    EpisodeRecord,
    EntityVisit,
    InMemoryMemoryRepository,
    MemoryContextPackage,
    MemoryContextKey,
    MemoryRepository,
    RelevantTurn,
    TurnReference,
    WorkingFact,
    WorkingMemory,
)
from src.core.memory.persistence import ThreadMemoryState
from src.core.memory.long_term import RetrievedLongTermMemory
from src.core.memory.routing_state import SessionRoutingState


logger = logging.getLogger(__name__)

_WORKING_FACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "analyst_name",
        re.compile(
            r"\b(?:remember\s*[,;:]?\s*)?(?:i(?:'m|m|\s+am)|my\s+(?:analyst\s+)?name\s+is|call\s+me)\s+([A-Za-z][A-Za-z .'-]{0,63}?)(?=\s+for\s+(?:this|the)\s+(?:conversation|investigation)|[,.]|$)",
            re.IGNORECASE,
        ),
    ),
    (
        "investigation_tag",
        re.compile(r"\b(?:investigation\s+)?tag\s+(?:is|=|:)\s*([A-Za-z0-9][A-Za-z0-9._-]{0,63})", re.IGNORECASE),
    ),
    (
        "owner_validation",
        re.compile(r"\bowner\s+(?:validation(?:\s+status)?|status)\s+(?:is|=|:)\s*([^,.;]{1,80})", re.IGNORECASE),
    ),
    (
        "identity_contradiction",
        re.compile(
            r"\b(?:identity\s+)?contradiction\s+(?:is|=|:)\s*(.{1,300}?)(?=\.\s+(?:keep|remember)|,\s*(?:and\s+)?(?:keep|remember)|$)",
            re.IGNORECASE,
        ),
    ),
    (
        "analyst_note",
        re.compile(r"\banalyst\s+note\s*(?:is|=|:)\s*(.{1,240}?)(?=\.|$)", re.IGNORECASE),
    ),
)

_EXPLICIT_MEMORY_PREFIX = re.compile(
    r"^\s*(?:also\s+)?(?:remember|note|keep\s+in\s+mind|retain|store)"
    r"(?:\s+for\s+(?:this|the)\s+(?:conversation|investigation))?"
    r"\s*(?:that\b)?\s*[,;:]?\s*",
    re.IGNORECASE,
)
_MEMORY_SEGMENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
MAX_EXACT_WORKING_FACT_CHARS = 600


def extract_working_facts(message: str) -> tuple[WorkingFact, ...]:
    """Extract only explicit bounded session facts; never infer from model prose."""
    facts: list[WorkingFact] = []
    for key, pattern in _WORKING_FACT_PATTERNS:
        match = pattern.search(message or "")
        if not match:
            continue
        value = " ".join(match.group(1).strip().rstrip(".").split())
        if value:
            facts.append(WorkingFact(key=key, value=value))
    for segment in _MEMORY_SEGMENT_SPLIT.split(message or ""):
        prefix = _EXPLICIT_MEMORY_PREFIX.match(segment)
        if not prefix:
            continue
        if any(pattern.search(segment) for _key, pattern in _WORKING_FACT_PATTERNS):
            continue
        value = re.sub(r"\s+", " ", segment[prefix.end() :].strip()).rstrip(".?!").strip()
        if not value or len(value) > MAX_EXACT_WORKING_FACT_CHARS:
            continue
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        facts.append(
            WorkingFact(
                key=f"remembered_statement:{digest}",
                value=value,
                fact_type="user_provided",
            )
        )
    return tuple(facts)


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
    memory_context: MemoryContextPackage | None = None


class MemoryStore:
    def __init__(
        self,
        max_messages: int,
        repository: MemoryRepository | None = None,
        *,
        max_episodes: int = 20,
        max_working_facts: int = 20,
        relevant_turn_limit: int | None = None,
    ) -> None:
        self.max_messages = max(0, max_messages)
        self.max_working_facts = max(1, int(max_working_facts))
        self.relevant_turn_limit = max(
            1,
            int(
                relevant_turn_limit
                if relevant_turn_limit is not None
                else max(1, self.max_messages // 2)
            ),
        )
        self._history_staging_limit = max(self.max_messages + 2, self.relevant_turn_limit * 2)
        self._history: dict[str, list[dict[str, str]]] = {}
        self._latest_completed_turns: dict[str, list[dict[str, str]]] = {}
        self._summaries: dict[str, dict[str, Any]] = {}
        self._recorded_request_ids: dict[str, set[str]] = {}
        self._turns: dict[str, list[RelevantTurn]] = {}
        self.repository = repository or InMemoryMemoryRepository(max_episodes)

    def get(self, session_id: str) -> list[dict[str, str]]:
        return list(self._history.get(session_id, []))

    def append(self, session_id: str, role: str, content: str) -> None:
        if self.max_messages == 0:
            return
        history = self._history.setdefault(session_id, [])
        history.append({"role": role, "content": content})
        self._history[session_id] = history[-self._history_staging_limit :]

    def clear_session(self, session_id: str) -> None:
        """Remove process-local continuity when an identity binding changes."""
        self._history.pop(session_id, None)
        self._latest_completed_turns.pop(session_id, None)
        self._summaries.pop(session_id, None)
        self._recorded_request_ids.pop(session_id, None)
        self._turns.pop(session_id, None)
        self.repository.clear_session(session_id)

    def upsert_working_facts(
        self,
        session_id: str,
        context_key: MemoryContextKey,
        facts: tuple[WorkingFact, ...],
        *,
        request_id: str = "",
    ) -> None:
        """Persist bounded same-conversation facts without promoting them to LTM."""
        if not facts:
            return
        working = self.repository.get_working(session_id)
        if working is None:
            working = WorkingMemory(
                session_id=session_id,
                context_key=context_key,
                episode_id=EpisodeRecord.create(session_id, context_key).episode_id,
            )
        merged = {
            (item.key, item.scope, item.entity_ids): item
            for item in working.working_facts
        }
        for fact in facts:
            if (
                fact.key != "analyst_name"
                and fact.scope == "conversation"
                and context_key.entities
            ):
                fact = replace(
                    fact,
                    scope="entity",
                    entity_ids=context_key.entities,
                )
            merged[(fact.key, fact.scope, fact.entity_ids)] = fact
        working.working_facts = tuple(merged.values())[-self.max_working_facts :]
        self.repository.set_working(working)
        logger.info(
            "event=working_facts_persisted request_id=%s write_count=%s working_fact_count=%s",
            request_id,
            len(facts),
            len(working.working_facts),
        )

    def set_investigation_baseline(
        self,
        session_id: str,
        context_key: MemoryContextKey,
        baseline: InvestigationBaseline,
        *,
        request_id: str = "",
    ) -> bool:
        """Replace a matching episode baseline only with a complete newer snapshot."""
        working = self.repository.get_working(session_id)
        if (
            working is None
            or working.context_key != context_key
            or set(baseline.entity_ids) != set(context_key.entities)
            or not baseline.owner_id
            or not baseline.projections
            or any(
                item.completeness != "complete" or item.truncated
                for item in baseline.projections
            )
        ):
            return False
        existing = working.baseline
        if existing is not None:
            if (
                existing.owner_id != baseline.owner_id
                or existing.captured_at > baseline.captured_at
            ):
                return False
            merged = {item.identity: item for item in existing.projections}
            merged.update({item.identity: item for item in baseline.projections})
            projections = tuple(merged.values())
            used_bytes = sum(
                len(json.dumps(
                    item.payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"))
                for item in projections
            )
            if (
                len(projections) > MAX_BASELINE_PROJECTIONS
                or used_bytes > MAX_BASELINE_TOTAL_BYTES
                or any(
                    len(json.dumps(
                        item.payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")) > MAX_BASELINE_PROJECTION_BYTES
                    for item in projections
                )
            ):
                return False
            baseline = InvestigationBaseline(
                entity_ids=baseline.entity_ids,
                captured_at=baseline.captured_at,
                source_request_id=baseline.source_request_id,
                scope=baseline.scope,
                projections=projections,
                owner_id=baseline.owner_id,
            )
        working.baseline = baseline
        self.repository.set_working(working)
        logger.info(
            "event=investigation_baseline_persisted request_id=%s entity=%s projection_count=%s",
            request_id,
            ",".join(baseline.entity_ids) or "none",
            len(baseline.projections),
        )
        return True

    def investigation_baselines(
        self,
        session_id: str,
        context_key: MemoryContextKey,
        *,
        owner_id: str,
    ) -> tuple[InvestigationBaseline, ...]:
        """Return bounded owner/context-compatible active and archived baselines."""
        if not owner_id:
            return ()
        selected: list[InvestigationBaseline] = []
        working = self.repository.get_working(session_id)
        if (
            working is not None
            and working.context_key == context_key
            and working.baseline is not None
            and working.baseline.owner_id == owner_id
        ):
            selected.append(working.baseline)
        for episode in reversed(self.repository.list_episodes(session_id)):
            baseline = episode.baseline
            if (
                episode.context_key != context_key
                or baseline is None
                or baseline.owner_id != owner_id
                or baseline.source_request_id in {
                    item.source_request_id for item in selected
                }
            ):
                continue
            selected.append(baseline)
            if len(selected) >= 4:
                break
        return tuple(selected)

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
        request_id: str = "",
        mutate_operational_episode: bool = True,
    ) -> None:
        """Record one bounded turn without storing provider payloads or prompts."""
        if self.max_messages == 0:
            return
        if request_id:
            recorded = self._recorded_request_ids.setdefault(session_id, set())
            if request_id in recorded:
                logger.info(
                    "event=memory_turn_skipped request_id=%s session_id=%s reason=idempotent_replay",
                    request_id,
                    session_id,
                )
                return
        working = self.repository.get_working(session_id)
        opened = mutate_operational_episode and (
            working is None or working.context_key != context_key
        )
        if mutate_operational_episode and (
            working is None or working.context_key != context_key
        ):
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
        if mutate_operational_episode and working is not None:
            working.latest_user_turn = compact_preview(user_content, limit=400)
            working.latest_assistant_turn = compact_preview(assistant_content, limit=600)
            working.last_providers = tuple(dict.fromkeys(providers))
            working.last_scope = scope
            working.limitations = tuple(dict.fromkeys(compact_preview(item, limit=200) for item in limitations))[:8]
            self.repository.set_working(working)
            logger.info(
                "event=%s request_id=%s episode_ref=%s provider_count=%s limitation_count=%s",
                "episode_opened" if opened else "episode_updated",
                request_id,
                working.episode_id,
                len(working.last_providers),
                len(working.limitations),
            )
        else:
            logger.info(
                "event=memory_recall_turn_recorded request_id=%s operational_episode_mutated=false",
                request_id,
            )
        now = datetime.now(timezone.utc).isoformat()
        self._turns.setdefault(session_id, []).append(
            RelevantTurn(
                request_id=request_id,
                context_key=context_key,
                user_content=user_content,
                assistant_content=assistant_content,
                created_at=now,
                retrieval_reason="raw_recent_turn",
                estimated_tokens=approx_tokens(user_content) + approx_tokens(assistant_content),
            )
        )
        self._turns[session_id] = self._turns[session_id][-self.relevant_turn_limit :]
        if request_id:
            recorded.add(request_id)

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
        long_term_memories: tuple[RetrievedLongTermMemory, ...] = (),
        activate_context: bool = True,
        thread_recall: bool = False,
    ) -> ConversationSnapshot:
        transitioned = False
        previous_episode_summary_included = False
        if context_key is not None and activate_context:
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
        package = self.compose_memory_context(
            session_id,
            settings,
            context_key=context_key,
            active_entities=routing_state.active_entities,
            request_id=request_id,
            long_term_memories=long_term_memories,
            thread_recall=thread_recall,
            entity_timeline=routing_state.entity_timeline,
        )
        messages = package.model_messages()
        recent = [item for item in messages if item.get("role") in {"user", "assistant"}]
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
            memory_context=package,
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

    def compose_memory_context(
        self,
        session_id: str,
        settings: Settings,
        *,
        context_key: MemoryContextKey | None,
        active_entities: tuple[str, ...] = (),
        request_id: str = "",
        long_term_memories: tuple[RetrievedLongTermMemory, ...] = (),
        thread_recall: bool = False,
        entity_timeline: tuple[EntityVisit, ...] = (),
    ) -> MemoryContextPackage:
        """Select bounded same-conversation continuity without model or embedding calls."""
        started = datetime.now(timezone.utc)
        max_turns = max(0, int(getattr(settings, "memory_relevant_turn_limit", 4)))
        turn_budget = max(0, int(getattr(settings, "memory_relevant_turn_token_budget", 900)))
        episode_limit = max(0, int(getattr(settings, "memory_episode_context_limit", 2)))
        episode_budget = max(0, int(getattr(settings, "memory_episode_context_token_budget", 300)))
        total_budget = max(0, int(getattr(settings, "memory_context_token_budget", 1400)))
        long_term_budget = max(
            0,
            int(getattr(settings, "memory_context_long_term_token_budget", 500)),
        )
        working = self.repository.get_working(session_id) or WorkingMemory(
            session_id=session_id,
            context_key=context_key or MemoryContextKey(),
            episode_id="",
        )
        summary = str(self._summaries.get(session_id, {}).get("text") or "")
        if (
            not thread_recall
            and context_key is not None
            and working.context_key != context_key
        ):
            # A scoped historical read may inspect an archived episode, but it
            # must not borrow the active episode's summary or activate it.
            summary = ""
        summary_tokens = approx_tokens(summary)
        omitted: list[str] = []
        if summary_tokens > total_budget:
            summary = ""
            summary_tokens = 0
            omitted.append("working_summary_over_budget")

        candidates = list(self._turns.get(session_id, ()))
        # Legacy raw messages have no context key. Once typed episodes exist,
        # relabeling those messages with the current key can reattach an old episode.
        if not candidates and not self.repository.list_episodes(session_id):
            history = self.get(session_id)
            legacy_key = context_key or MemoryContextKey(topic_family="general")
            for index in range(0, len(history) - 1):
                user = history[index]
                assistant = history[index + 1]
                if user.get("role") != "user" or assistant.get("role") != "assistant":
                    continue
                user_content = str(user.get("content") or "")
                assistant_content = str(assistant.get("content") or "")
                candidates.append(
                    RelevantTurn(
                        request_id=f"legacy-{index}",
                        context_key=legacy_key,
                        user_content=user_content,
                        assistant_content=assistant_content,
                        created_at=f"legacy-{index:08d}",
                        retrieval_reason="raw_recent_turn",
                        estimated_tokens=approx_tokens(user_content) + approx_tokens(assistant_content),
                        source_representation="raw",
                    )
                )
        active = set(active_entities)
        if thread_recall:
            active.clear()
        elif context_key is not None and context_key.topic_family == "general":
            active.clear()
            candidates = [
                item for item in candidates if item.context_key.topic_family == "general"
            ]
        elif context_key is not None and context_key.entities:
            current_entities = set(context_key.entities)
            candidates = [
                item
                for item in candidates
                if item.context_key == context_key
                or bool(current_entities.intersection(item.context_key.entities))
            ]

        # The setting is an exact raw-message target, not a completed-turn count.
        # Non-target messages remain eligible through bounded digests.
        deduplicated: dict[str, RelevantTurn] = {}
        for item in candidates:
            key = item.request_id or hashlib.sha256(
                f"{item.user_content}\0{item.assistant_content}".encode("utf-8")
            ).hexdigest()
            existing = deduplicated.get(key)
            if existing is None or item.created_at >= existing.created_at:
                deduplicated[key] = item
        candidates = list(deduplicated.values())
        raw_message_target = max(
            0,
            int(getattr(settings, "conversation_recent_raw_messages", 0)),
        )
        ordered_candidates = sorted(candidates, key=lambda item: item.created_at)
        raw_eligible: list[tuple[str, str]] = []
        for item in ordered_candidates:
            if item.source_representation != "raw":
                continue
            raw_eligible.extend(((item.request_id, "user"), (item.request_id, "assistant")))
        raw_targets = set(raw_eligible[-raw_message_target:]) if raw_message_target else set()
        layered_candidates: list[RelevantTurn] = []
        for item in candidates:
            user_raw = (item.request_id, "user") in raw_targets
            assistant_raw = (item.request_id, "assistant") in raw_targets
            user_content = item.user_content if user_raw else compact_preview(item.user_content, limit=360)
            assistant_content = item.assistant_content if assistant_raw else compact_preview(
                item.assistant_content, limit=560
            )
            representation = "raw" if user_raw and assistant_raw else "digest" if not user_raw and not assistant_raw else "mixed"
            layered_candidates.append(
                replace(
                    item,
                    user_content=user_content,
                    assistant_content=assistant_content,
                    estimated_tokens=approx_tokens(user_content) + approx_tokens(assistant_content),
                    source_representation=representation,
                    user_source_representation="raw" if user_raw else "digest",
                    assistant_source_representation="raw" if assistant_raw else "digest",
                )
            )
        candidates = layered_candidates

        def priority(item: RelevantTurn) -> tuple[int, int, int, str]:
            exact = int(context_key is not None and item.context_key == context_key)
            entity_match = int(bool(active.intersection(item.context_key.entities)))
            investigation = int(item.context_key.topic_family != "general")
            return exact, entity_match, investigation, item.created_at

        ranked = sorted(candidates, key=priority, reverse=True)
        selected: list[RelevantTurn] = []
        used_turn_tokens = 0
        downgraded_raw_messages = 0
        for candidate in ranked:
            if len(selected) >= max_turns:
                break
            if used_turn_tokens + candidate.estimated_tokens > turn_budget:
                downgraded_raw_messages += sum(
                    source == "raw" for source in (
                        candidate.user_source_representation,
                        candidate.assistant_source_representation,
                    )
                )
                user_digest = compact_preview(candidate.user_content, limit=180)
                assistant_digest = compact_preview(candidate.assistant_content, limit=260)
                candidate = replace(
                    candidate,
                    user_content=user_digest,
                    assistant_content=assistant_digest,
                    estimated_tokens=approx_tokens(user_digest) + approx_tokens(assistant_digest),
                    source_representation="digest",
                    user_source_representation="digest",
                    assistant_source_representation="digest",
                )
            if used_turn_tokens + candidate.estimated_tokens > turn_budget:
                continue
            reason = (
                "thread_recall"
                if thread_recall
                else "active_topic"
                if context_key is not None and candidate.context_key == context_key
                else "active_entity"
                if active.intersection(candidate.context_key.entities)
                else "recent_investigation"
                if candidate.context_key.topic_family != "general"
                else "recent_turn"
            )
            selected.append(
                RelevantTurn(
                    **{**candidate.__dict__, "retrieval_reason": reason}
                )
            )
            used_turn_tokens += candidate.estimated_tokens
        selected.sort(key=lambda item: item.created_at)

        episodes = list(self.repository.list_episodes(session_id))
        episode_entities = (
            set(context_key.entities)
            if context_key is not None and context_key.entities
            else active
        )
        matching_episodes = [
            item
            for item in reversed(episodes)
            if item.compact_summary
            and item.compact_summary != summary
            and (
                thread_recall
                or context_key is None
                or item.context_key == context_key
                or bool(episode_entities.intersection(item.context_key.entities))
            )
        ]
        selected_episodes: list[EpisodeRecord] = []
        used_episode_tokens = 0
        for episode in matching_episodes:
            tokens = approx_tokens(episode.compact_summary)
            if len(selected_episodes) >= episode_limit:
                break
            if used_episode_tokens + tokens > episode_budget:
                continue
            selected_episodes.append(episode)
            used_episode_tokens += tokens

        used = summary_tokens + used_turn_tokens + used_episode_tokens
        while selected and used > total_budget:
            removed = selected.pop(0)
            used -= removed.estimated_tokens
            omitted.append("older_relevant_turn_over_budget")
        while selected_episodes and used > total_budget:
            removed = selected_episodes.pop()
            used -= approx_tokens(removed.compact_summary)
            omitted.append("episode_summary_over_budget")

        selected_long_term: list[RetrievedLongTermMemory] = []
        used_long_term_tokens = 0
        seen_statements: set[str] = set()
        for item in long_term_memories:
            normalized = item.memory.statement.strip().casefold()
            if not normalized or normalized in seen_statements:
                continue
            if used_long_term_tokens + item.estimated_tokens > long_term_budget:
                omitted.append("long_term_memory_over_budget")
                continue
            selected_long_term.append(item)
            seen_statements.add(normalized)
            used_long_term_tokens += item.estimated_tokens

        fact_candidates = list(working.working_facts)
        if thread_recall:
            for episode in reversed(self.repository.list_episodes(session_id)):
                fact_candidates.extend(episode.working_facts)
        selected_facts: list[WorkingFact] = []
        seen_facts: set[tuple[Any, ...]] = set()
        for item in fact_candidates:
            fact_key = (
                item.key,
                item.scope,
                item.entity_ids,
            )
            if fact_key in seen_facts:
                continue
            if (
                thread_recall
                or item.scope == "conversation"
                or context_key is None
                or not item.entity_ids
                or bool(set(item.entity_ids).intersection(context_key.entities))
            ):
                selected_facts.append(item)
                seen_facts.add(fact_key)
        selected_timeline = tuple(entity_timeline[-8:]) if thread_recall else ()
        timeline_tokens = sum(
            approx_tokens(
                f"visit {item.sequence}: {','.join(item.ordered_entity_ids)} {item.task_family}"
            )
            for item in selected_timeline
        )
        fact_budget = max(0, total_budget - summary_tokens - timeline_tokens)
        bounded_facts: list[WorkingFact] = []
        fact_tokens = 0
        for item in selected_facts:
            tokens = approx_tokens(f"{item.key}: {item.value}")
            if fact_tokens + tokens > fact_budget:
                omitted.append("working_fact_over_budget")
                continue
            bounded_facts.append(item)
            fact_tokens += tokens
        selected_facts = bounded_facts
        while selected and used + fact_tokens + timeline_tokens > total_budget:
            removed = selected.pop(0)
            used -= removed.estimated_tokens
            omitted.append("older_relevant_turn_over_budget")
        while selected_episodes and used + fact_tokens + timeline_tokens > total_budget:
            removed = selected_episodes.pop()
            used -= approx_tokens(removed.compact_summary)
            omitted.append("episode_summary_over_budget")
        package = MemoryContextPackage(
            working_summary=summary,
            relevant_turns=tuple(selected),
            episode_summaries=tuple(selected_episodes),
            long_term_memories=tuple(selected_long_term),
            working_facts=tuple(selected_facts),
            entity_timeline=selected_timeline,
            active_entities=tuple(active_entities),
            estimated_tokens=max(0, used + used_long_term_tokens),
            omitted=tuple(dict.fromkeys(omitted)),
            thread_recall_requested=thread_recall,
            # Local storage is bounded by retention and token budgets, so it
            # cannot independently prove exhaustive thread coverage.
            thread_recall_complete=False,
            sources_considered=tuple(name for name, present in (
                ("working_summary", bool(summary)),
                ("relevant_turns", bool(candidates)),
                ("episodes", bool(episodes)),
                ("working_facts", bool(fact_candidates)),
                ("entity_timeline", bool(entity_timeline)),
                ("product_ltm_selection", bool(long_term_memories)),
            ) if present),
        )
        package = MemoryContextPackage(
            **{
                **package.__dict__,
                "estimated_tokens": package.estimated_tokens + fact_tokens + timeline_tokens,
            }
        )
        latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        logger.info(
            "event=recent_turns_selected request_id=%s candidate_count=%s selected_count=%s "
            "configured_raw_message_target=%s raw_messages_selected=%s "
            "raw_messages_downgraded=%s digest_messages_selected=%s "
            "estimated_tokens=%s latency_ms=%s",
            request_id,
            len(candidates),
            len(selected),
            raw_message_target,
            sum(
                source == "raw"
                for item in selected
                for source in (
                    item.user_source_representation,
                    item.assistant_source_representation,
                )
            ),
            downgraded_raw_messages,
            sum(
                source == "digest"
                for item in selected
                for source in (
                    item.user_source_representation,
                    item.assistant_source_representation,
                )
            ),
            used_turn_tokens,
            latency_ms,
        )
        logger.info(
            "event=memory_context_composed request_id=%s selected_turns=%s raw_turn_count=%s digest_turn_count=%s summary_tokens=%s working_fact_count=%s episode_count=%s long_term_count=%s thread_recall=%s thread_recall_complete=%s memory_context_truncated=%s estimated_tokens=%s omitted_count=%s",
            request_id,
            len(selected),
            sum(
                source == "raw"
                for item in selected
                for source in (item.user_source_representation, item.assistant_source_representation)
            ),
            sum(
                source == "digest"
                for item in selected
                for source in (item.user_source_representation, item.assistant_source_representation)
            ),
            summary_tokens,
            len(package.working_facts),
            len(selected_episodes),
            len(selected_long_term),
            thread_recall,
            package.thread_recall_complete,
            bool(package.omitted),
            package.estimated_tokens,
            len(package.omitted),
        )
        logger.info(
            "event=long_term_memory_context_composed request_id=%s selected_count=%s estimated_tokens=%s omitted_count=%s",
            request_id,
            len(selected_long_term),
            used_long_term_tokens,
            len(package.omitted),
        )
        return package

    def durable_components(
        self,
        session_id: str,
        *,
        turn_limit: int,
        episode_limit: int,
    ) -> dict[str, Any]:
        """Export only approved bounded components for a ThreadMemoryState."""
        working = self.repository.get_working(session_id)
        summary = self._summaries.get(session_id, {})
        references = tuple(
            TurnReference(
                request_id=item.request_id,
                context_key=item.context_key,
                created_at=item.created_at,
                user_digest=compact_preview(item.user_content, limit=360),
                assistant_digest=compact_preview(item.assistant_content, limit=560),
                workflow_status="completed",
            )
            for item in self._turns.get(session_id, ())[-max(0, turn_limit) :]
        )
        return {
            "working_memory": working,
            "recent_turn_references": references,
            "recent_episodes": tuple(self.repository.list_episodes(session_id)[-max(0, episode_limit) :]),
            "summary_updated_at": str(summary.get("summary_updated_at") or ""),
            "summary_source_request_id": str(summary.get("summary_source_request_id") or ""),
            "summary_size_tokens": approx_tokens(str(summary.get("text") or "")),
        }

    def restore_durable_state(
        self,
        state: ThreadMemoryState,
        transcript: tuple[Any, ...] = (),
    ) -> None:
        """Restore compact state and owned transcript rows after process recreation."""
        session_id = state.session_id
        self.clear_session(session_id)
        if state.working_memory is not None:
            self.repository.set_working(state.working_memory)
            if state.working_memory.compact_summary:
                self._summaries[session_id] = {
                    "text": state.working_memory.compact_summary,
                    "summary_updated_at": state.summary_updated_at,
                    "summary_source_request_id": state.summary_source_request_id,
                    "summary_source_message_count": len(state.recent_turn_references) * 2,
                }
        for episode in state.recent_episodes:
            self.repository.add_episode(episode)

        references = tuple(state.recent_turn_references)
        if state.summary_source_request_id:
            summary_index = next(
                (index for index, item in enumerate(references)
                 if item.request_id == state.summary_source_request_id),
                None,
            )
            if summary_index is not None:
                references = (
                    references[summary_index + 1 :]
                    if summary_index < len(references) - 1
                    else references[summary_index:]
                )
        refs = {item.request_id: item for item in references}
        grouped: dict[str, dict[str, Any]] = {}
        uncorrelated: list[Any] = []
        for message in transcript:
            request = str(getattr(message, "request_id", ""))
            role = str(getattr(message, "role", ""))
            if request in refs and role in {"user", "assistant"}:
                grouped.setdefault(request, {})[role] = message
            elif not request and role in {"user", "assistant"}:
                uncorrelated.append(message)

        complete_pairs: list[tuple[Any, Any]] = []
        pending_user: Any | None = None
        for message in uncorrelated:
            role = str(getattr(message, "role", ""))
            if role == "user":
                pending_user = message
            elif role == "assistant" and pending_user is not None:
                complete_pairs.append((pending_user, message))
                pending_user = None
        unmatched = [
            reference for reference in references
            if not {"user", "assistant"}.issubset(grouped.get(reference.request_id, {}))
        ]
        for reference, pair in zip(unmatched[-len(complete_pairs) :], complete_pairs[-len(unmatched) :]):
            grouped[reference.request_id] = {"user": pair[0], "assistant": pair[1]}

        restored: list[RelevantTurn] = []
        for reference in references:
            pair = grouped.get(reference.request_id, {})
            if "user" in pair and "assistant" in pair:
                user_content = str(pair["user"].content)
                assistant_content = str(pair["assistant"].content)
                retrieval_reason = "raw_recent_turn"
                self.append(session_id, "user", user_content)
                self.append(session_id, "assistant", assistant_content)
            elif reference.user_digest and reference.assistant_digest:
                user_content = reference.user_digest
                assistant_content = reference.assistant_digest
                retrieval_reason = "durable_turn_digest"
            else:
                continue
            restored.append(
                RelevantTurn(
                    request_id=reference.request_id,
                    context_key=reference.context_key,
                    user_content=user_content,
                    assistant_content=assistant_content,
                    created_at=reference.created_at,
                    retrieval_reason=retrieval_reason,
                    estimated_tokens=(
                        approx_tokens(user_content) + approx_tokens(assistant_content)
                    ),
                    source_representation=(
                        "raw" if retrieval_reason == "raw_recent_turn" else "digest"
                    ),
                )
            )
        self._turns[session_id] = restored
        if restored:
            latest = restored[-1]
            self._latest_completed_turns[session_id] = [
                {"role": "user", "content": latest.user_content},
                {"role": "assistant", "content": latest.assistant_content},
            ]
            self._recorded_request_ids[session_id] = {
                item.request_id for item in restored if item.request_id
            }
            working = self.repository.get_working(session_id)
            if working is not None:
                working.latest_user_turn = compact_preview(latest.user_content, limit=400)
                working.latest_assistant_turn = compact_preview(latest.assistant_content, limit=600)
                self.repository.set_working(working)

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

        archived_candidates = sum(
            1
            for episode in self.repository.list_episodes(session_id)
            if episode.context_key == context_key
        )
        logger.info(
            "event=episode_lookup_started request_id=%s session_id=%s target_entities=%s "
            "active_episode_ref=%s archived_candidate_count=%s",
            request_id,
            session_id,
            ",".join(context_key.entities) or "none",
            working.episode_id,
            archived_candidates,
        )
        latest_completed = self._latest_completed_turn(self.get(session_id))
        if latest_completed:
            self._latest_completed_turns[session_id] = latest_completed
        self._archive_current_episode(session_id, working, settings)
        self._history[session_id] = []
        self._summaries.pop(session_id, None)
        matching = self.repository.take_episode(session_id, context_key)
        if matching:
            self._summaries[session_id] = {
                "text": matching.compact_summary,
                "summary_updated_at": matching.updated_at,
                "summary_source_message_count": 0,
            }
        episode = matching or EpisodeRecord.create(session_id, context_key)
        restored_facts = {
            (item.key, item.scope, item.entity_ids): item
            for item in (matching.working_facts if matching else ())
        }
        for item in working.working_facts:
            if item.scope == "conversation":
                restored_facts[(item.key, item.scope, item.entity_ids)] = item
        self.repository.set_working(
            WorkingMemory(
                session_id=session_id,
                context_key=context_key,
                episode_id=episode.episode_id,
                compact_summary=matching.compact_summary if matching else "",
                last_providers=matching.last_providers if matching else (),
                last_scope=matching.last_scope if matching else "none",
                limitations=matching.limitations if matching else (),
                working_facts=tuple(restored_facts.values()),
                baseline=matching.baseline if matching else None,
            )
        )
        logger.info(
            "event=episode_lookup_completed request_id=%s session_id=%s selected_episode_ref=%s "
            "selected_episode_state=%s selected_entities=%s reason=%s",
            request_id,
            session_id,
            episode.episode_id,
            "archived" if matching else "new",
            ",".join(context_key.entities) or "none",
            "exact_context_match" if matching else "no_archived_match",
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
        context_turns = [
            item
            for item in self._turns.get(session_id, ())
            if item.context_key == working.context_key
        ]
        history = [
            message
            for turn in context_turns
            for message in (
                {"role": "user", "content": turn.user_content},
                {"role": "assistant", "content": turn.assistant_content},
            )
        ] or self.get(session_id)
        summary = self._summaries.get(session_id, {}).get("text", "")
        if history:
            payload = self._build_summary_payload(
                history,
                SessionRoutingState(active_entities=working.context_key.entities),
                route=None,
                graph_context=None,
            )
            payload["context"] = working.context_key.identity
            payload["evidence_scope"] = {
                "scope": working.last_scope,
                "providers": list(working.last_providers),
            }
            if summary:
                try:
                    prior_summary = json.loads(summary)
                except (TypeError, ValueError):
                    prior_summary = {}
                if isinstance(prior_summary, dict):
                    for key in (
                        "sticky_user_facts",
                        "contradictions",
                        "inferred_role",
                        "key_findings",
                        "next_checks",
                        "limitations",
                    ):
                        if prior_summary.get(key):
                            payload[key] = prior_summary[key]
            summary = self._serialize_summary_payload(
                payload,
                max_tokens=max(1, settings.conversation_summary_max_tokens),
            )
        episode_facts = tuple(
            item
            for item in working.working_facts
            if item.scope == "conversation"
            or not item.entity_ids
            or bool(set(item.entity_ids).intersection(working.context_key.entities))
        )
        self.repository.add_episode(
            EpisodeRecord(
                episode_id=working.episode_id,
                session_id=session_id,
                context_key=working.context_key,
                compact_summary=summary,
                working_facts=episode_facts,
                inferred_role=tuple(payload.get("inferred_role", ())) if history else (),
                key_findings=tuple(payload.get("key_findings", ())) if history else (),
                contradictions=tuple(dict.fromkeys((
                    *(payload.get("contradictions", ()) if history else ()),
                    *(item.value for item in episode_facts if item.key == "identity_contradiction"),
                ))),
                unresolved_questions=tuple(payload.get("unresolved_questions", ())) if history else (),
                next_checks=tuple(payload.get("next_checks", ())) if history else (),
                evidence_scope=tuple(
                    f"{key}={value}" for key, value in (payload.get("evidence_scope", {}) if history else {}).items()
                ),
                limitations=working.limitations,
                last_providers=working.last_providers,
                last_scope=working.last_scope,
                turn_count=sum(1 for item in history if item.get("role") == "user"),
                baseline=working.baseline,
            )
        )
        logger.info(
            "event=episode_closed episode_ref=%s turn_count=%s summary_tokens=%s",
            working.episode_id,
            sum(1 for item in history if item.get("role") == "user"),
            approx_tokens(summary),
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
        retention_pressure = len(history) > self.max_messages
        if tokens <= settings.conversation_summary_trigger_tokens and not retention_pressure:
            return False

        # This setting is exactly a raw-message count. Pair continuity that no
        # longer fits remains available through durable bounded turn digests.
        recent_count = max(0, int(settings.conversation_recent_raw_messages))
        older = history[:-recent_count] if recent_count else history
        recent = history[-recent_count:] if recent_count else []
        existing = self._summaries.get(session_id)
        source_messages = older or history
        if not older and existing and int(existing.get("summary_source_message_count", 0)) >= len(history):
            return False
        summary_payload = self._build_summary_payload(
            source_messages, routing_state, route=route, graph_context=graph_context
        )
        if existing:
            try:
                prior = json.loads(str(existing.get("text") or ""))
            except (TypeError, ValueError):
                prior = {}
            if isinstance(prior, dict):
                summary_payload = self._merge_summary_payload(prior, summary_payload)
        text = self._serialize_summary_payload(
            summary_payload,
            max_tokens=max(1, settings.conversation_summary_max_tokens),
        )
        summary_source_request_id = request_id
        turns = self._turns.get(session_id, ())
        recent_turn_count = sum(1 for item in recent if item.get("role") == "user")
        if older and len(turns) > recent_turn_count:
            summary_source_request_id = turns[-recent_turn_count - 1].request_id
        elif not older and turns:
            summary_source_request_id = turns[-1].request_id
        self._summaries[session_id] = {
            "text": text,
            "summary_updated_at": datetime.now(timezone.utc).isoformat(),
            "summary_source_message_count": len(source_messages),
            "summary_source_request_id": summary_source_request_id,
        }
        self._history[session_id] = recent
        working = self.repository.get_working(session_id)
        if working is not None:
            working.compact_summary = text
            self.repository.set_working(working)
        logger.info(
            "event=conversation_summary_updated request_id=%s session_id=%s source_messages=%s recent_messages=%s summary_tokens=%s tokens_before=%s retention_pressure=%s",
            request_id,
            session_id,
            len(source_messages),
            len(recent),
            approx_tokens(text),
            tokens,
            retention_pressure,
        )
        return True

    def _history_tokens(self, session_id: str) -> int:
        return sum(approx_tokens(item.get("content", "")) for item in self.get(session_id))

    @staticmethod
    def _merge_summary_payload(prior: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        merged = dict(prior)
        for key, value in current.items():
            if isinstance(value, list):
                previous = merged.get(key)
                values = [*(previous if isinstance(previous, list) else ()), *value]
                merged[key] = list(dict.fromkeys(str(item) for item in values if str(item).strip()))[-8:]
            elif value not in (None, "", {}, ()):
                merged[key] = value
        return merged

    @staticmethod
    def _serialize_summary_payload(payload: dict[str, Any], *, max_tokens: int) -> str:
        """Keep valid deterministic JSON within the configured token estimate."""
        budget = max(1, int(max_tokens))
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if approx_tokens(text) <= budget:
            return text
        compact: dict[str, Any] = {}
        for key in (
            "active_entities", "sticky_user_facts", "contradictions", "inferred_role",
            "key_findings", "next_checks", "limitations", "evidence_scope",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                compact[key] = [compact_preview(str(item), limit=100) for item in value[:2]]
            elif value:
                compact[key] = value
        text = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        for key in ("evidence_scope", "limitations", "next_checks", "key_findings", "inferred_role"):
            if approx_tokens(text) <= budget:
                return text
            compact.pop(key, None)
            text = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        for key in ("contradictions", "sticky_user_facts", "active_entities"):
            values = compact.get(key)
            if isinstance(values, list):
                compact[key] = [compact_preview(str(item), limit=60) for item in values]
        text = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        while approx_tokens(text) > budget and compact:
            changed = False
            for key in ("contradictions", "sticky_user_facts", "active_entities"):
                values = compact.get(key)
                if isinstance(values, list) and values:
                    values.pop(0)
                    changed = True
                    if not values:
                        compact.pop(key, None)
                    break
            if not changed:
                compact.pop(next(iter(compact)))
            text = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        return text if approx_tokens(text) <= budget else "{}"

    @staticmethod
    def _build_summary_payload(
        older: list[dict[str, str]],
        routing_state: SessionRoutingState,
        *,
        route: Any | None,
        graph_context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        older_user_messages = [
            compact_preview(item.get("content", ""), limit=360)
            for item in older
            if item.get("role") == "user"
        ][-5:]
        sticky_user_facts = [
            compact_preview(item.get("content", ""), limit=360)
            for item in older
            if item.get("role") == "user"
            and re.search(r"\b(?:remember\s+my|my\s+name\s+is)\b", item.get("content", ""), re.IGNORECASE)
        ][-3:]
        assistant_sentences = [
            sentence.strip()
            for item in older
            if item.get("role") == "assistant"
            for sentence in re.split(r"(?<=[.!?])\s+|\n+", str(item.get("content") or ""))
            if sentence.strip()
        ]

        def matching(*terms: str, limit: int = 3) -> list[str]:
            return list(
                dict.fromkeys(
                    compact_preview(sentence, limit=220)
                    for sentence in assistant_sentences
                    if any(term in sentence.casefold() for term in terms)
                )
            )[:limit]

        contradictions = matching("contradict", "conflict", " vs ")
        inferred_role = matching("role", "fingerprint", "classified", "classification", limit=2)
        next_checks = matching("next check", "verify", "confirm", "investigate", limit=3)
        limitations = matching("unavailable", "not observed", "limited", "truncated", "unknown", limit=3)
        key_findings = list(dict.fromkeys(compact_preview(item, limit=220) for item in assistant_sentences))[-4:]
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
            "sticky_user_facts": sticky_user_facts,
            "contradictions": contradictions,
            "inferred_role": inferred_role,
            "key_findings": key_findings[:5],
            "next_checks": next_checks,
            "evidence_scope": previous_route,
            "limitations": limitations,
            "investigation_topic": previous_route.get("intent") or routing_state.previous_intent or "",
            "known_findings": known_findings[:5],
            "previous_routes": [previous_route] if previous_route else [],
            "older_user_requests": older_user_messages,
            "unresolved_questions": [item for item in older_user_messages if "?" in item][-3:],
            "user_constraints": [],
        }
