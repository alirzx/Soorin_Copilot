"""Deterministic Evidence Reviewer V1."""

from __future__ import annotations

from src.core.agent.contracts import EvidencePack, ReviewDecision, TaskSpec, ToolResult


class EvidenceReviewer:
    def review(self, task: TaskSpec, results: list[ToolResult]) -> ReviewDecision:
        required_cardinality = {
            "asset.get_profile": (1, 1),
            "asset.get_detection": (1, 1),
            "graph.get_summary": (1, 1),
            "graph.get_neighbors": (1, 1),
            "graph.get_relationship": (2, 2),
            "graph.compare_assets": (2, 2),
            "graph.find_path": (2, 2),
            "knowledge.search": (0, 2),
        }
        invalid_bindings = tuple(
            capability
            for capability in task.required_capabilities
            if capability in required_cardinality
            and not required_cardinality[capability][0]
            <= len(task.entities)
            <= required_cardinality[capability][1]
        )
        if invalid_bindings:
            return ReviewDecision(
                outcome="missing_required_evidence",
                reasons=("Required capability entity binding was not available.",),
                missing_capabilities=invalid_bindings,
            )
        by_capability = {result.source_capability: result for result in results}
        missing = tuple(
            capability
            for capability in task.required_capabilities
            if capability not in by_capability
        )
        if missing:
            return ReviewDecision(
                outcome="missing_required_evidence",
                reasons=("One or more required capabilities were not executed.",),
                missing_capabilities=missing,
            )
        required = [by_capability[name] for name in task.required_capabilities]
        if required and all(
            result.status in {"unavailable", "not_configured", "invalid"}
            for result in required
        ):
            return ReviewDecision(
                outcome="safe_failure",
                reasons=("All required evidence capabilities were unavailable or invalid.",),
            )
        failed = tuple(
            result.source_capability
            for result in required
            if result.status in {"unavailable", "not_configured", "invalid"}
        )
        if failed:
            return ReviewDecision(
                outcome="missing_required_evidence",
                reasons=("Required evidence was unavailable.",),
                missing_capabilities=failed,
            )
        limitations: list[str] = []
        for result in required:
            if result.status in {"partial", "empty", "not_found"}:
                limitations.append(f"{result.source_capability} status is {result.status}.")
            if result.freshness in {"stale", "unknown"}:
                limitations.append(f"{result.source_capability} freshness is {result.freshness}.")
            if result.completeness != "complete" or result.truncated:
                limitations.append(f"{result.source_capability} evidence is incomplete or truncated.")
            if not result.context_included:
                limitations.append(f"{result.source_capability} evidence was not included in model context.")
            if result.contradictions:
                limitations.append(f"{result.source_capability} reported contradictions.")
            limitations.extend(result.limitations)
        if limitations:
            return ReviewDecision(
                outcome="answer_with_limitations",
                reasons=tuple(dict.fromkeys(limitations)),
            )
        return ReviewDecision(outcome="sufficient")

    def build_pack(self, task: TaskSpec, results: list[ToolResult]) -> EvidencePack:
        return EvidencePack(
            task=task,
            tool_results=tuple(results),
            facts=tuple(fact for result in results for fact in result.facts),
            limitations=tuple(
                dict.fromkeys(limitation for result in results for limitation in result.limitations)
            ),
            contradictions=tuple(
                dict.fromkeys(item for result in results for item in result.contradictions)
            ),
        )
