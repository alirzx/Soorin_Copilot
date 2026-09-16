"""Compatibility exports for the canonical Phase 4C workflow nodes."""

from src.core.agent.phase4c_nodes import (
    Phase4CWorkflowNodes,
    _deepened_entities,
    _phase4c_deepening_results,
)

__all__ = [
    "Phase4CWorkflowNodes",
    "_deepened_entities",
    "_phase4c_deepening_results",
]
