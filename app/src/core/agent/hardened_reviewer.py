"""Evidence-review refinements for intentionally bounded ranked searches."""

from __future__ import annotations

from dataclasses import replace

from src.core.agent.contracts import ReviewDecision, TaskSpec, ToolResult
from src.core.agent.reviewer import EvidenceReviewer as BaseEvidenceReviewer
from src.core.graph.structured import StructuredQueryMode


_RANKED_TRUNCATION = "graph.search_assets evidence is incomplete or truncated."


class EvidenceReviewer(BaseEvidenceReviewer):
    """Do not classify a two-row top/tie check as missing search evidence."""

    def review(
        self,
        task: TaskSpec,
        results: list[ToolResult],
        *,
        allow_supplemental: bool = False,
    ) -> ReviewDecision:
        decision = super().review(
            task,
            results,
            allow_supplemental=allow_supplemental,
        )
        query = task.structured_query
        if (
            query is None
            or query.mode is not StructuredQueryMode.SEARCH
            or query.sort is None
            or query.direction is None
            or query.limit != 2
        ):
            return decision

        search = next(
            (
                result.structured_asset_set
                for result in results
                if result.source_capability == "graph.search_assets"
                and result.structured_asset_set is not None
            ),
            None,
        )
        if search is None or getattr(search, "returned_count", 0) < 2:
            return decision
        remaining_material = tuple(
            item for item in decision.material_limitations if item != _RANKED_TRUNCATION
        )
        if remaining_material:
            return decision
        caveats = tuple(
            dict.fromkeys(
                (
                    *decision.caveats,
                    "The ranked search intentionally retained the leading two rows to check the top value and possible tie; additional lower-ranked matches were not needed for focal selection.",
                )
            )
        )
        return replace(
            decision,
            outcome="sufficient",
            reasons=(),
            limitations=caveats,
            caveats=caveats,
            material_limitations=(),
        )
