"""Request-scoped human-readable trace rendering for Copilot requests."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger(__name__)


@dataclass
class CopilotRequestTrace:
    request_id: str
    session_id: str
    message_preview: str
    total_latency_ms: int = 0
    status: str = "ok"
    warnings: int = 0
    errors: int = 0
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)

    def put(self, section: str, **values: Any) -> None:
        current = self.sections.setdefault(section, {})
        current.update(values)


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value)
    return str(value)


def render_human_copilot_trace(trace: CopilotRequestTrace) -> None:
    """Emit one local, request-scoped multiline summary."""
    lines = [
        "==================== COPILOT REQUEST ====================",
        f"request_id : {trace.request_id}",
        f"session_id : {trace.session_id}",
        f"message    : {trace.message_preview}",
    ]
    order = [
        "REQUEST",
        "ROUTER INPUT",
        "ROUTING STATE",
        "ENTITY",
        "ENTITY BINDING",
        "INTENT",
        "ROUTING",
        "ASSET PROFILE",
        "ASSET DETECTION",
        "GRAPH RETRIEVAL",
        "MODEL INPUT",
        "MODEL RESPONSE",
        "STATE UPDATE",
        "RESULT",
    ]
    for section in order:
        values = trace.sections.get(section)
        if not values:
            continue
        lines.append("")
        lines.append(f"== {section} ==")
        width = max(len(key) for key in values.keys()) if values else 1
        for key, value in values.items():
            lines.append(f"{key:<{width}}: {_format_value(value)}")
    lines.extend(
        [
            "",
            "================== COPILOT REQUEST END ==================",
        ]
    )
    logger.info("\n%s", "\n".join(lines))
