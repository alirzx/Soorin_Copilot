"""Request-scoped, bounded human-readable traces for Copilot requests."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO

from src.core.graph.structured import semantic_query_identity, structured_query_identity


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
    "IDENTITY AND ENTITY RESOLUTION",
    "ROUTING DECISION",
    "STRUCTURED QUERY",
    "TASK AND PLAN",
    "MEMORY SUFFICIENCY AND EVIDENCE GAP",
    "ADAPTIVE AGENT LOOP",
    "LANGGRAPH WORKFLOW",
    "SPECIALISTS",
    "CAPABILITY EXECUTION",
    "EVIDENCE COVERAGE",
    "CONTEXT AND TOKEN BUDGET",
    "MEMORY TRANSITION",
    "LLM CALLS",
    "FINAL STATUS",
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
    task_plan = trace.sections.get("TASK AND PLAN", {})
    adaptive = trace.sections.get("ADAPTIVE AGENT LOOP", {})
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
        f"{ok} {plan.get('step_count', task_plan.get('step_count', 0))} bounded step(s)"
        f"{divider}{plan.get('source', task_plan.get('plan_source', 'deterministic'))}"
        f"{divider}{task_plan.get('orchestration_mode', 'direct')}",
        "",
        _paint("EXECUTION", "36", color),
        f"{ok} {capabilities.get('statuses') or 'no live capability required'}",
        *(
            [
                f"{ok if adaptive.get('status') == 'completed' else warning} AGENT   "
                f"turns={adaptive.get('agent_turns', 0)}{divider}"
                f"stop={adaptive.get('stop_reason') or 'running'}"
            ]
            if adaptive
            else []
        ),
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
        label = str(
            item.get("capability")
            or item.get("specialist")
            or item.get("node")
            or item.get("provider")
            or f"step_{index + 1}"
        ).upper()
        lines.append(f"{branch} {label}")
        for key, value in _safe_items(item):
            if key in {"capability", "specialist", "node", "provider"}:
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
        details = values.get("details")
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


def trace_from_investigation_state(state: dict[str, Any]) -> CopilotRequestTrace:
    """Adapt final bounded workflow state into the established safe trace renderer."""
    request_id = str(state.get("request_id") or "")
    trace = CopilotRequestTrace(
        request_id=request_id,
        session_id=str(state.get("session_id") or ""),
        message_preview=str(state.get("message") or "").strip().replace("\n", " ")[:120],
        status=str(state.get("workflow_status") or "unknown"),
        warnings=len((state.get("final_response") or {}).get("_warnings") or ()),
    )
    resolution = state.get("resolved_entities")
    resolved = tuple(getattr(resolution, "entities", ()) or ())
    route = state.get("routing_result")
    task = state.get("task")
    plan = state.get("execution_plan")
    results = list(state.get("tool_results") or ())
    pack = state.get("evidence_pack")
    review = state.get("review_decision")
    synthesis = state.get("synthesis_result") or {}
    memory = state.get("memory_update_result") or {}
    node_records = list(state.get("node_records") or ())[-24:]
    specialist_records = list(state.get("specialist_records") or ())
    limitations = list(state.get("limitation_reasons") or ())

    trace.put(
        "REQUEST",
        trace_id=state.get("trace_id"),
        session_id=state.get("session_id"),
        streaming=bool(state.get("streaming")),
    )
    trace.put(
        "IDENTITY AND ENTITY RESOLUTION",
        status=getattr(resolution, "status", "unknown"),
        entities=[getattr(item, "value", "") for item in resolved],
        authority_sources=[getattr(item, "source", "") for item in resolved],
        reference_detected=bool(getattr(resolution, "reference_detected", False)),
    )
    trace.put(
        "ROUTING DECISION",
        intent=getattr(route, "intent", "unknown"),
        scope=getattr(route, "scope", "none"),
        direction=getattr(route, "direction", "none"),
        depth=getattr(route, "depth", 0),
        decision_source=getattr(route, "decision_source", "unknown"),
        fallback_used=bool(getattr(route, "fallback_used", False)),
        router_latency_ms=getattr(route, "semantic_router_latency_ms", 0),
    )
    structured_query = getattr(task, "structured_query", None)
    if structured_query is not None:
        structured_results = [
            item
            for item in results
            if getattr(item, "structured_asset_set", None) is not None
        ]
        structured_result = structured_results[0] if structured_results else None
        evidence = (
            getattr(structured_result, "structured_asset_set", None)
            if structured_result is not None
            else None
        )
        provider_context = getattr(
            getattr(structured_result, "provider_result", None),
            "context",
            {},
        )
        if not isinstance(provider_context, dict):
            provider_context = {}
        semantic_id = semantic_query_identity(structured_query)
        evidence_query_id = getattr(evidence, "query_identity", "")
        expected_evidence_id = (
            structured_query_identity(
                structured_query,
                active_graph_version=getattr(evidence, "active_graph_version", None),
            )
            if evidence is not None
            else ""
        )
        filters = structured_query.filters.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
            exclude_defaults=True,
            exclude={"predicate"},
        )
        predicate = structured_query.filters.predicate
        predicate_shape = None
        if predicate is not None:
            depth, leaves = predicate.shape()
            predicate_shape = f"depth={depth},leaves={leaves}"
        group_fields = structured_query.group_by_fields or (
            (structured_query.group_by,) if structured_query.group_by is not None else ()
        )
        reference_kind = getattr(
            getattr(route, "structured_result_reference", None),
            "kind",
            "none",
        )
        identity_match = (
            evidence is not None
            and evidence_query_id == expected_evidence_id
            and getattr(evidence, "semantic_query_id", "") == semantic_id
            and getattr(structured_result, "normalized_query_hash", "")
            == expected_evidence_id
            and getattr(structured_result, "context_identity", "")
            == expected_evidence_id
        )
        trace.put(
            "STRUCTURED QUERY",
            query_source=getattr(route, "decision_source", "unknown"),
            mode=structured_query.mode.value,
            filters=filters,
            predicate_shape=predicate_shape,
            group_by=(structured_query.group_by.value if structured_query.group_by else None),
            group_by_fields=[item.value for item in group_fields],
            sort=(structured_query.sort.value if structured_query.sort else None),
            direction=(structured_query.direction.value if structured_query.direction else None),
            router_supplied_limit=structured_query.router_supplied_limit,
            user_explicit_limit=structured_query.user_explicit_limit,
            normalized_limit=structured_query.limit,
            runtime_effective_limit=provider_context.get("runtime_effective_limit"),
            runtime_max_limit=provider_context.get("runtime_max_limit"),
            requested_output_fields=[
                item.value for item in structured_query.requested_output_fields
            ],
            reference_kind=reference_kind,
            previous_set_used=reference_kind == "set_query",
            fresh_rerun=structured_query.fresh_rerun,
            semantic_class=structured_query.semantic_class,
            mapping_mode=structured_query.class_mapping_mode,
            selector_fields=[
                item.value for item in structured_query.class_selector_fields
            ],
            semantic_query_id=semantic_id,
            evidence_query_identity=evidence_query_id,
            tool_normalized_query_hash=getattr(
                structured_result, "normalized_query_hash", ""
            ),
            tool_context_identity=getattr(structured_result, "context_identity", ""),
            identity_match=identity_match,
        )
    trace.put(
        "TASK AND PLAN",
        workflow_mode=getattr(task, "workflow_mode", "unknown"),
        orchestration_mode=getattr(task, "orchestration_mode", state.get("orchestration_mode", "direct")),
        planner_called=bool(state.get("planner_called")),
        plan_id=getattr(plan, "plan_id", ""),
        plan_source=getattr(plan, "source", ""),
        validated=bool(getattr(plan, "validated", False)),
        step_count=len(getattr(plan, "steps", ()) or ()),
    )
    gap_plan = state.get("evidence_gap_plan")
    trace.put(
        "MEMORY SUFFICIENCY AND EVIDENCE GAP",
        requirement_count=len(getattr(getattr(gap_plan, "requirements", None), "requirements", ()) or ()),
        decisions=[item.decision for item in (getattr(gap_plan, "decisions", ()) or ())],
        reasons=[item.reason_code for item in (getattr(gap_plan, "decisions", ()) or ())],
        skipped_capabilities=list(getattr(gap_plan, "skipped_capabilities", ()) or ()),
        selected_views=[
            f"{item.capability}:{','.join(item.views)}"
            for item in (getattr(gap_plan, "view_selections", ()) or ())
        ],
    )
    loop = state.get("agent_loop_state")
    if loop is not None:
        observations = tuple(getattr(loop, "observations", ()) or ())
        budget = getattr(loop, "budget", None)
        detail = getattr(getattr(state.get("task"), "orchestration_mode", None), "value", None)
        trace.put(
            "ADAPTIVE AGENT LOOP",
            orchestration_mode=str(detail or state.get("orchestration_mode") or "adaptive"),
            status="completed" if getattr(loop, "stop_reason", None) else "running",
            stop_reason=getattr(loop, "stop_reason", None),
            agent_turns=getattr(budget, "investigator_turns", 0),
            llm_calls=getattr(budget, "llm_calls", 0),
            tool_calls=getattr(budget, "capability_calls", 0),
            details=[
                {
                    "turn": item.turn,
                    "capabilities": [request.capability for request in item.capability_requests],
                    "statuses": [status for _capability, status in item.status_summary],
                    "evidence_refs_added": len(item.new_evidence_references),
                    "evidence_refs_changed": len(item.changed_evidence_references),
                    "gaps_resolved": len(item.new_coverage),
                    "remaining_gaps": len(item.remaining_gap_ids),
                    "execution_skipped": bool(item.rejected_actions),
                    "safe_rejection_reason": item.rejected_actions[0] if item.rejected_actions else None,
                    "material_progress": item.material_progress,
                    "context_tokens_before": item.context_input_tokens_before,
                    "context_tokens_after": item.context_input_tokens_after,
                }
                for item in observations[:8]
            ],
        )
    trace.put(
        "LANGGRAPH WORKFLOW",
        status=state.get("workflow_status"),
        node_count=len(node_records),
        details=[
            {
                "node": item.get("node"),
                "status": item.get("status"),
                "latency_ms": item.get("latency_ms"),
                "next_edge": item.get("next_edge"),
            }
            for item in node_records
        ],
    )
    trace.put("SPECIALISTS", details=specialist_records)
    trace.put(
        "CAPABILITY EXECUTION",
        count=len(results),
        details=[
            {
                "capability": item.source_capability,
                "status": item.status,
                "provider": item.provider,
                "cache_status": item.cache_status,
                "latency_ms": item.latency_ms,
                "truncated": item.truncated,
            }
            for item in results[:12]
        ],
    )
    trace.put(
        "EVIDENCE COVERAGE",
        review_outcome=getattr(review, "outcome", None),
        graph_completeness=getattr(pack, "graph_completeness", None),
        included=sum(bool(item.context_included) for item in results),
        missing=list(getattr(pack, "missing_evidence", ()) or ()),
    )
    context_review = state.get("context_review") or {}
    trace.put(
        "CONTEXT AND TOKEN BUDGET",
        decision=context_review.get("decision"),
        input_tokens=context_review.get("input_tokens"),
        remaining_usable_tokens=context_review.get("remaining_usable_tokens"),
        required_context_missing=context_review.get("required_context_missing"),
    )
    active = state.get("active_entity_state")
    trace.put(
        "MEMORY TRANSITION",
        completed=bool(memory.get("completed")),
        memory_write_count=memory.get("memory_write_count"),
        working_fact_write_count=memory.get("working_fact_write_count"),
        ltm_candidate_processed_count=memory.get("ltm_candidate_processed_count"),
        thread_state_persistence_attempted=memory.get(
            "thread_state_persistence_attempted"
        ),
        active_entities=list(getattr(active, "active_entities", ()) or ()),
        previous_scope=getattr(active, "previous_scope", None),
    )
    router_calls = (
        1 + int(getattr(route, "semantic_router_retry_count", 0) or 0)
        if bool(getattr(route, "semantic_router_called", False))
        else 0
    )
    planner_calls = int(bool(state.get("planner_called")))
    investigator_calls = int(getattr(getattr(loop, "budget", None), "investigator_turns", 0))
    synthesis_calls = int(bool(synthesis) and synthesis.get("provider") != "deterministic")
    usage = synthesis.get("usage") or {}
    trace.put(
        "LLM CALLS",
        router_calls=router_calls,
        planner_calls=planner_calls,
        investigator_calls=investigator_calls,
        specialist_calls=0,
        synthesis_calls=synthesis_calls,
        total_calls=router_calls + planner_calls + investigator_calls + synthesis_calls,
        synthesis_latency_ms=synthesis.get("latency_ms", 0),
        prompt_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"),
        completion_tokens=usage.get("completion_tokens") or usage.get("output_tokens"),
    )
    trace.put(
        "FINAL STATUS",
        status=state.get("workflow_status"),
        limitation_reasons=limitations,
        terminal=bool(state.get("terminal")),
    )
    return trace


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
