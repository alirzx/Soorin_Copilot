"""Safe model-facing projection of registered read-only capabilities."""

from __future__ import annotations

from typing import Any, Iterable

from src.core.agent.contracts import CapabilitySpec


def project_capability_schema(spec: CapabilitySpec) -> dict[str, Any]:
    """Expose only planner-visible authority and bounded input schema fields."""
    schema = spec.input_schema.model_json_schema()
    allowed_arguments = list(spec.allowed_arguments or tuple(schema.get("properties", {})))
    properties = schema.get("properties", {})
    return {
        "name": spec.name,
        "purpose": spec.description,
        "minimum_entities": spec.required_entity_cardinality[0],
        "maximum_entities": spec.required_entity_cardinality[1],
        "allowed_arguments": allowed_arguments,
        "argument_schema": {
            "type": "object",
            "properties": {
                name: properties[name]
                for name in allowed_arguments
                if name in properties
            },
            "additionalProperties": False,
        },
        "allowed_views": list(spec.allowed_views),
        "allowed_detail_levels": list(spec.allowed_detail_levels),
        "allowed_knowledge_purposes": list(spec.allowed_purposes),
        "allowed_scopes": list(spec.allowed_scopes),
        "allowed_depths": list(spec.allowed_depths),
        "dependencies": list(spec.dependencies),
        "parallelization": spec.parallelization,
        "reusable_locally": spec.reusable_locally,
        "read_only": spec.read_only,
    }


def project_capability_catalog(
    capabilities: Iterable[CapabilitySpec],
    *,
    allowed_names: Iterable[str] | None = None,
) -> tuple[dict[str, Any], ...]:
    allowed = set(allowed_names) if allowed_names is not None else None
    return tuple(
        project_capability_schema(spec)
        for spec in capabilities
        if spec.planner_visible
        and spec.read_only
        and (allowed is None or spec.name in allowed)
    )
