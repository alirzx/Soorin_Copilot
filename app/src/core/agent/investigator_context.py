"""Compact, deterministic context construction for Investigator decisions."""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any

from src.core.agent.capability_projection import project_capability_catalog
from src.core.agent.contracts import (
    AgentLoopBudget,
    AgentObservation,
    EvidenceLedger,
    RequestConstraints,
    TaskEnvelope,
    TaskSpec,
)
from src.core.agent.registry import CapabilityRegistry
from src.core.llm.token_estimator import TokenEstimator


class InvestigatorContextError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class InvestigatorContext:
    context_json: str
    raw_estimate: int
    calibrated_estimate: int
    output_reservation: int
    compacted: bool
    input_tokens_before: int
    input_tokens_after: int
    compacted_tokens: int
    evidence_reference_count: int
    delta_count: int
    capability_schema_count: int
    remaining_hard_budget: int


class InvestigatorContextBuilder:
    """Build decision context without copying raw Product/Graph/RAG payloads."""

    def __init__(self, settings: Any, registry: CapabilityRegistry) -> None:
        self.settings = settings
        self.registry = registry
        deployment = settings.deployment_for_purpose("investigator")
        self.estimator = TokenEstimator(
            deployment=deployment.name,
            model=deployment.model,
            multiplier=settings.llm_token_estimate_multiplier,
        )

    def build(
        self,
        *,
        task: TaskSpec,
        constraints: RequestConstraints,
        envelope: TaskEnvelope,
        ledger: EvidenceLedger,
        latest_observation: AgentObservation | None,
        budget: AgentLoopBudget,
        long_term_selection: Any = None,
        turn: int,
        system_prompt: str = "",
    ) -> InvestigatorContext:
        allowed_names = tuple(dict.fromkeys(
            capability
            for gap in ledger.gaps
            if gap.status == "open"
            for capability in gap.authorized_capabilities
        )) or tuple(dict.fromkeys((*task.required_capabilities, *task.optional_capabilities)))
        capability_catalog = list(project_capability_catalog(
            self.registry.list(planner_visible=True),
            allowed_names=allowed_names,
        ))
        payload: dict[str, Any] = {
            "task": {
                "request": task.request,
                "intent": task.intent,
                "scope": task.scope,
                "direction": task.direction,
                "graph_depth": task.graph_depth,
                "relationship_mode": task.relationship_mode,
                "temporal_mode": task.temporal_mode,
                "evidence_mode": task.evidence_mode,
                "required_capabilities": list(task.required_capabilities),
                "optional_capabilities": list(task.optional_capabilities),
            },
            "request_constraints": {
                "allow_live": constraints.allow_live,
                "require_current": constraints.require_current,
                "memory_only": constraints.memory_only,
                "reason_codes": list(constraints.reason_codes),
            },
            "task_envelope": {
                "authorized_entities": list(envelope.ordered_entities),
                "temporal_scope": envelope.temporal_scope,
                "freshness_requirement": envelope.freshness_requirement,
                "comparison_required": envelope.comparison_required,
            },
            "memory_observations": self._memory_observations(long_term_selection),
            "evidence_ledger": self._ledger(ledger),
            "latest_observation": self._observation(latest_observation),
            "remaining_budget": {
                "turn": turn,
                "investigator_turns": budget.remaining_turns,
                "llm_calls": budget.remaining_llm_calls,
                "capability_calls": budget.remaining_capability_calls,
                "deepened_entities": max(0, budget.max_deepened_entities - len(budget.deepened_entities)),
                "graph_depth": budget.max_graph_depth,
                "deadline_seconds": max(0, int(budget.deadline_monotonic - time.monotonic())),
            },
            "capability_catalog": capability_catalog,
        }
        rendered, estimate = self._render(payload, system_prompt)
        input_tokens_before = estimate.calibrated_tokens
        compacted = False
        if estimate.calibrated_tokens > self.settings.investigator_max_input_tokens:
            compacted = True
            self._compact_payload(payload, latest_observation, aggressive=False)
            rendered, estimate = self._render(payload, system_prompt)
        if estimate.calibrated_tokens > self.settings.investigator_max_input_tokens:
            compacted = True
            self._compact_payload(payload, latest_observation, aggressive=True)
            rendered, estimate = self._render(payload, system_prompt)
        if estimate.calibrated_tokens > self.settings.investigator_hard_input_tokens:
            compacted = True
            self._authority_only(payload)
            rendered, estimate = self._render(payload, system_prompt)
        if estimate.calibrated_tokens > self.settings.investigator_hard_input_tokens:
            raise InvestigatorContextError(
                "context_budget_exhausted",
                "Required Investigator authority context exceeds the configured hard limit.",
            )
        return InvestigatorContext(
            context_json=rendered,
            raw_estimate=estimate.raw_tokens,
            calibrated_estimate=estimate.calibrated_tokens,
            output_reservation=self.settings.investigator_max_tokens,
            compacted=compacted,
            input_tokens_before=input_tokens_before,
            input_tokens_after=estimate.calibrated_tokens,
            compacted_tokens=max(0, input_tokens_before - estimate.calibrated_tokens),
            evidence_reference_count=len(ledger.evidence_references),
            delta_count=self._delta_count(latest_observation),
            capability_schema_count=len(capability_catalog),
            remaining_hard_budget=max(
                0,
                self.settings.investigator_hard_input_tokens - estimate.calibrated_tokens,
            ),
        )

    def _compact_payload(
        self,
        payload: dict[str, Any],
        latest_observation: AgentObservation | None,
        *,
        aggressive: bool,
    ) -> None:
        payload["memory_observations"] = (
            [] if aggressive else payload["memory_observations"][:2]
        )
        ledger = payload["evidence_ledger"]
        latest_ids = {
            self._display_reference_id(item)
            for item in (
                (*latest_observation.new_evidence_references, *latest_observation.changed_evidence_references)
                if latest_observation is not None
                else ()
            )
        }
        open_gap_ids = {
            item["gap_id"] for item in ledger["gaps"] if item["status"] == "open"
        }
        references = sorted(
            ledger["evidence_references"],
            key=lambda item: (
                item["reference_id"] not in latest_ids,
                not bool(set(item["covered_gap_ids"]) & open_gap_ids),
                not item["reusable"],
                item["reference_id"],
            ),
        )
        ledger["evidence_references"] = references[:8 if aggressive else 12]
        ledger["gaps"] = [
            item for item in ledger["gaps"]
            if item["status"] != "satisfied"
        ]
        ledger["contradictions"] = ledger["contradictions"][:4 if aggressive else 8]
        ledger["coverage_limitations"] = ledger["coverage_limitations"][:4 if aggressive else 8]
        ledger["failures"] = ledger["failures"][:4 if aggressive else 8]
        ledger["attempted_capabilities"] = [] if aggressive else ledger["attempted_capabilities"][-8:]
        ledger["capability_coverage"] = [] if aggressive else ledger["capability_coverage"][-8:]

    @staticmethod
    def _authority_only(payload: dict[str, Any]) -> None:
        payload["memory_observations"] = []
        ledger = payload["evidence_ledger"]
        ledger["evidence_references"] = []
        ledger["contradictions"] = []
        ledger["coverage_limitations"] = []
        ledger["failures"] = []
        ledger["attempted_capabilities"] = []
        ledger["capability_coverage"] = []
        ledger["gaps"] = [item for item in ledger["gaps"] if item["status"] != "satisfied"]

    def _render(self, payload: dict[str, Any], system_prompt: str) -> tuple[str, Any]:
        rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        estimate = self.estimator.estimate_messages((
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": rendered},
        ))
        return rendered, estimate

    @staticmethod
    def _memory_observations(selection: Any) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        for item in tuple(getattr(selection, "memories", ()) or ())[:5]:
            memory = getattr(item, "memory", None)
            observations.append({
                "memory_id": str(getattr(memory, "memory_id", ""))[:80],
                "entities": list(tuple(getattr(memory, "entity_ids", ()) or ())[:2]),
                "evidence_refs": list(tuple(getattr(memory, "evidence_refs", ()) or ())[:8]),
                "epistemic_status": str(getattr(memory, "epistemic_status", "unknown"))[:40],
                "freshness": str(getattr(item, "freshness", "unknown"))[:40],
            })
        return observations

    @staticmethod
    def _ledger(ledger: EvidenceLedger) -> dict[str, Any]:
        return {
            "authorized_entities": list(ledger.authorized_entities),
            "structured_candidates": list(ledger.structured_candidates),
            "attempted_capabilities": list(ledger.attempted_capabilities),
            "capability_coverage": [list(item) for item in ledger.capability_coverage],
            "contradictions": list(ledger.contradictions),
            "gaps": [
                {
                    "gap_id": gap.gap_id,
                    "dimension": gap.dimension,
                    "importance": gap.importance,
                    "status": gap.status,
                    "temporal_requirement": gap.temporal_requirement,
                    "authorized_capabilities": list(gap.authorized_capabilities),
                    "authority_requirement": gap.authority_requirement,
                    "entities": list(gap.entities),
                }
                for gap in ledger.gaps
            ],
            "coverage_limitations": list(ledger.coverage_limitations),
            "failures": list(ledger.failures),
            "evidence_references": [
                {
                    "reference_id": InvestigatorContextBuilder._display_reference_id(item.reference_id),
                    "capability": item.source_capability,
                    "authority": item.authority_class,
                    "temporal_class": item.temporal_class,
                    "entities": list(item.entities),
                    "status": item.status,
                    "freshness": item.freshness,
                    "completeness": item.completeness,
                    "evidence_classes": list(item.evidence_classes),
                    "covered_gap_ids": list(item.covered_gap_ids),
                    "limitation_flags": list(item.material_limitation_flags),
                    "selected_views": list(item.selected_views),
                    "structured_query": bool(item.structured_query_identity),
                    "active_graph_version_present": bool(item.active_graph_version),
                    "reusable": item.reusable,
                }
                for item in ledger.evidence_references
            ],
        }

    @staticmethod
    def _observation(observation: AgentObservation | None) -> dict[str, Any] | None:
        if observation is None:
            return None
        return {
            "turn": observation.turn,
            "capabilities": [item.capability for item in observation.capability_requests],
            "result_references": [
                InvestigatorContextBuilder._display_reference_id(item)
                for item in observation.result_references
            ],
            "status_summary": [list(item) for item in observation.status_summary],
            "new_coverage": list(observation.new_coverage),
            "new_contradictions": list(observation.new_contradictions),
            "resolved_contradictions": list(observation.resolved_contradictions),
            "new_evidence_references": [
                InvestigatorContextBuilder._display_reference_id(item)
                for item in observation.new_evidence_references
            ],
            "changed_evidence_references": [
                InvestigatorContextBuilder._display_reference_id(item)
                for item in observation.changed_evidence_references
            ],
            "rejected_actions": list(observation.rejected_actions),
            "remaining_gap_ids": list(observation.remaining_gap_ids),
            "material_progress": observation.material_progress,
            "tool_call_count": observation.tool_call_count,
            "budget_delta": [list(item) for item in observation.budget_delta],
        }

    @staticmethod
    def _display_reference_id(reference_id: str) -> str:
        return reference_id.rsplit(":", 1)[-1][:20]

    @staticmethod
    def _delta_count(observation: AgentObservation | None) -> int:
        if observation is None:
            return 0
        return sum(map(len, (
            observation.new_evidence_references,
            observation.changed_evidence_references,
            observation.new_coverage,
            observation.new_contradictions,
            observation.resolved_contradictions,
            observation.rejected_actions,
        )))
