"""Request-scoped, bounded human-readable traces for Copilot requests."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO


logger = logging.getLogger(__name__)
_SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "access_token",
    "password",
    "captcha",
    "secret",
    "reasoning_content",
    "raw_payload",
    "full_prompt",
    "assistant_response",
}
_DETAILED_SECTION_ORDER = (
    "REQUEST",
    "ROUTER INPUT",
    "ROUTING STATE",
    "ENTITY",
    "ENTITY BINDING",
    "INTENT",
    "ROUTING",
    "AGENT TASK",
    "WORKFLOW SELECTION",
    "PLANNER",
    "EXECUTION PLAN",
    "PLAN VALIDATION",
    "CAPABILITY STEPS",
    "ASSET PROFILE",
    "ASSET DETECTION",
    "GRAPH RETRIEVAL",
    "GRAPH COVERAGE",
    "KNOWLEDGE RETRIEVAL",
    "EVIDENCE COVERAGE",
    "REVIEW DECISION",
    "SUPPLEMENTAL RETRIEVAL",
    "CONTEXT",
    "MODEL INPUT",
    "TOKEN BUDGET",
    "SYNTHESIS",
    "MODEL RESPONSE",
    "SNAPSHOT",
    "STATE UPDATE",
    "RESULT",
)


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
    if isinstance(value, dict):
        items = list(value.items())[:12]
        rendered = ", ".join(f"{key}={_format_value(item)}" for key, item in items)
        return rendered + (", ..." if len(value) > len(items) else "")
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:12]
        rendered = ", ".join(_format_value(item) for item in items)
        return rendered + (", ..." if len(value) > len(items) else "")
    rendered = str(value).replace("\r", " ").replace("\n", " ")
    return rendered[:500] + ("..." if len(rendered) > 500 else "")


def _terminal_stream(output_logger: logging.Logger) -> TextIO:
    candidates = [output_logger, logging.getLogger()]
    for candidate in candidates:
        for handler in candidate.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                return getattr(handler, "stream", sys.stderr)
    return sys.stderr


def _supports_utf8(stream: TextIO) -> bool:
    encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
    return encoding in {"utf8", "utf_8"}


def _color_enabled(settings: Any, stream: TextIO) -> bool:
    if os.getenv("NO_COLOR") is not None or getattr(settings, "log_format", "console") != "console":
        return False
    mode = getattr(settings, "log_color", "auto")
    return mode == "always" or (mode == "auto" and bool(getattr(stream, "isatty", lambda: False)()))


def _paint(text: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


def _status_color(value: Any) -> str:
    normalized = str(value).lower()
    if normalized in {"error", "failed", "safe_failure", "unavailable", "missing_required_evidence"}:
        return "31"
    if normalized in {"partial", "warning", "answer_with_limitations", "not_found"}:
        return "33"
    return "32"


def _summary_trace(trace: CopilotRequestTrace, *, color: bool, utf8: bool) -> str:
    entity = trace.sections.get("ENTITY", {})
    intent = trace.sections.get("INTENT", {})
    plan = trace.sections.get("EXECUTION PLAN", {})
    capabilities = trace.sections.get("CAPABILITY STEPS", {})
    review = trace.sections.get("REVIEW DECISION", {})
    model = trace.sections.get("MODEL RESPONSE", {})
    result = trace.sections.get("RESULT", {})
    ok, warning, failed = ("✓", "⚠", "✗") if utf8 else ("OK", "WARN", "FAIL")
    separator = "═" if utf8 else "="
    divider = " · " if utf8 else " | "
    review_mark = warning if review.get("outcome") != "sufficient" else ok
    lines = [
        _paint(f"{separator * 8} REQUEST {trace.request_id} {separator * 8}", "36", color),
        f"message  {trace.message_preview}",
        "",
        _paint("ROUTING", "34", color),
        f"{ok} ENTITY   {entity.get('value') or 'none'}{divider}{entity.get('source') or 'none'}",
        f"{ok} ROUTER   {intent.get('intent') or trace.sections.get('AGENT TASK', {}).get('intent', 'unknown')}",
        "",
        _paint("PLAN", "34", color),
        f"{ok} {plan.get('step_count', 0)} bounded step(s){divider}{plan.get('source', 'deterministic')}",
        "",
        _paint("EXECUTION", "36", color),
        f"{ok} {capabilities.get('statuses') or 'no live capability required'}",
        "",
        _paint("EVIDENCE", "36", color),
        f"{review_mark} REVIEW   {review.get('outcome', 'not_required')}",
        "",
        _paint("SYNTHESIS", "34", color),
        f"{ok} {model.get('provider', 'deterministic')}{divider}{model.get('provider_latency_ms', 0)}ms",
        "",
        _paint("RESULT", _status_color(trace.status), color),
        f"{ok if trace.status == 'ok' else failed} {trace.status}{divider}{result.get('total_latency_ms', trace.total_latency_ms)}ms{divider}warnings={trace.warnings}",
    ]
    return "\n".join(lines)


def _display_name(section: str) -> str:
    return {
        "INTENT": "ROUTER",
        "REVIEW DECISION": "EVIDENCE REVIEW",
        "MODEL RESPONSE": "SYNTHESIS RESULT",
    }.get(section, section)


def _section_color(section: str, values: dict[str, Any]) -> str:
    if section in {"INTENT", "PLANNER", "WORKFLOW SELECTION", "SYNTHESIS", "MODEL RESPONSE"}:
        return "34"
    if section in {
        "CAPABILITY STEPS",
        "ASSET PROFILE",
        "ASSET DETECTION",
        "GRAPH RETRIEVAL",
        "GRAPH COVERAGE",
        "KNOWLEDGE RETRIEVAL",
    }:
        return "36"
    status = values.get("status") or values.get("outcome")
    return _status_color(status) if status else "36"


def _safe_items(values: dict[str, Any]) -> list[tuple[str, Any]]:
    return [
        (key, value)
        for key, value in list(values.items())[:40]
        if key.lower() not in _SENSITIVE_KEYS and value not in (None, "", [], (), {})
    ]


def _capability_lines(details: list[Any], *, utf8: bool) -> list[str]:
    lines: list[str] = []
    branch, stem = ("├─", "│ ") if utf8 else ("+-", "| ")
    for index, item in enumerate(details[:12]):
        if not isinstance(item, dict):
            continue
        label = str(item.get("capability") or item.get("provider") or f"step_{index + 1}").upper()
        lines.append(f"{branch} {label}")
        for key, value in _safe_items(item):
            if key in {"capability", "provider"}:
                continue
            lines.append(f"{stem} {_label(key):<27}: {_format_value(value)}")
        if index != len(details) - 1:
            lines.append(stem)
    return lines


def _label(key: str) -> str:
    aliases = {
        "semantic_router_latency_ms": "latency",
        "recommended_steps": "recommended steps",
        "selected_output_reservation": "output reservation",
        "llm_context_safety_margin_tokens": "configured safety margin",
        "estimate_to_actual_ratio": "estimate/actual ratio",
    }
    return aliases.get(key, key.replace("_", " "))[:27]


def _detailed_trace(trace: CopilotRequestTrace, *, color: bool, utf8: bool) -> str:
    separator = "═" if utf8 else "="
    lines = [
        _paint(f"{separator * 8} REQUEST {trace.request_id} {separator * 8}", "36", color),
        "",
        "REQUEST",
        f"{'message preview':<27}: {_format_value(trace.message_preview)}",
        f"{'session':<27}: {_format_value(trace.session_id)}",
    ]
    for key, value in _safe_items(trace.sections.get("REQUEST", {})):
        lines.append(f"{_label(key):<27}: {_format_value(value)}")
    rendered = {"REQUEST"}
    for section in _DETAILED_SECTION_ORDER:
        values = trace.sections.get(section)
        if not values or section == "REQUEST":
            continue
        safe_values = _safe_items(values)
        details = values.get("details") if section == "CAPABILITY STEPS" else None
        if not safe_values and not details:
            continue
        lines.extend(["", _paint(_display_name(section), _section_color(section, values), color)])
        if isinstance(details, list):
            lines.extend(_capability_lines(details, utf8=utf8))
        for key, value in safe_values:
            if key in {"details"}:
                continue
            lines.append(f"{_label(key):<27}: {_format_value(value)}")
        rendered.add(section)
    for section, values in trace.sections.items():
        if section in rendered or not values:
            continue
        lines.extend(["", _paint(section, _section_color(section, values), color)])
        for key, value in _safe_items(values):
            lines.append(f"{_label(key):<27}: {_format_value(value)}")
    return "\n".join(lines)


def render_human_copilot_trace(
    trace: CopilotRequestTrace,
    *,
    settings: Any | None = None,
    stream: TextIO | None = None,
    output_logger: logging.Logger | None = None,
    emit: bool = True,
) -> str:
    """Render and optionally emit one safe human trace in summary or detailed form."""
    if settings is None:
        from src.config.settings import get_settings

        settings = get_settings()
    output_logger = output_logger or logger
    stream = stream or _terminal_stream(output_logger)
    utf8 = _supports_utf8(stream)
    color = _color_enabled(settings, stream)
    detail = getattr(settings, "copilot_human_trace_detail", "detailed")
    block = (
        _summary_trace(trace, color=color, utf8=utf8)
        if detail == "summary"
        else _detailed_trace(trace, color=color, utf8=utf8)
    )
    if emit:
        output_logger.info("\n%s", block, extra={"soorin_human_trace": True})
        output_logger.info(
            "event=human_trace_rendered request_id=%s detail=%s status=%s",
            trace.request_id,
            detail,
            trace.status,
        )
    return block
