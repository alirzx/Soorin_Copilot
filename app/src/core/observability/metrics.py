"""Low-cardinality Prometheus metrics for Soorin Copilot operations."""

from __future__ import annotations

from collections.abc import Iterable
from threading import Lock
import time
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


FORBIDDEN_LABEL_NAMES = frozenset(
    {
        "request_id",
        "trace_id",
        "user_id",
        "session_id",
        "conversation_id",
        "memory_id",
        "ip",
        "ip_address",
        "hostname",
        "entity",
        "entity_id",
        "prompt",
        "prompt_text",
        "model_output",
        "route_text",
        "error_message",
    }
)

WORKFLOW_MODES = frozenset({"direct", "multi_step", "unknown"})
REQUEST_STATUSES = frozenset({"completed", "partial", "failed", "cancelled"})
MEMORY_KINDS = frozenset({"short_term", "exact", "semantic"})
MEMORY_RESULTS = frozenset({"hit", "miss", "unavailable"})
MEMORY_DECISIONS = frozenset({"reuse", "verify", "live", "conflict"})
MEMORY_ACTIONS = frozenset({"skip", "verify", "live"})
CONTEXT_SECTIONS = frozenset(
    {"profile", "detection", "graph", "memory", "knowledge", "history", "product"}
)
KNOWN_VIEWS = frozenset(
    {
        "none",
        "multiple",
        "overview",
        "identity",
        "security",
        "network",
        "activity",
        "evidence",
        "similarity",
        "cluster",
        "full",
    }
)

HTTP_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)
DEPENDENCY_DURATION_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)
LLM_DURATION_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 180, 300, 600)
WORKFLOW_DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 180, 300, 600)
STAGE_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300)
MEMORY_DURATION_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
CONTEXT_TOKEN_BUCKETS = (16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)

STREAM_STATUSES = frozenset({"completed", "error", "interrupted"})
PRODUCT_OPERATIONS = frozenset(
    {
        "login",
        "profile",
        "detection",
        "topology",
        "memory_thread_load",
        "memory_thread_save",
        "memory_search",
        "memory_get",
        "memory_create",
        "memory_transition",
        "memory_audit",
        "usage_report",
        "other",
    }
)
PRODUCT_STATUS_CLASSES = frozenset({"2xx", "3xx", "4xx", "5xx", "exception"})
MEMORY_LIFECYCLE_ACTIONS = frozenset(
    {
        "candidate_created",
        "promoted",
        "confirmed",
        "superseded",
        "conflict",
        "rejected",
        "invalidated",
        "expired",
        "deleted",
    }
)
OBSERVATION_RESULTS = frozenset({"success", "failure"})
MEMORY_REVISION_OPERATIONS = frozenset({"thread_save", "memory_transition", "index_status", "other"})
MEMORY_CANONICAL_SOURCES = frozenset({"product", "local", "other"})
MEMORY_VECTOR_OPERATIONS = frozenset({"search", "index", "delete", "reconcile", "other"})
WORKFLOW_FALLBACK_KINDS = frozenset({"routing", "plan"})


def _bounded(value: object, allowed: Iterable[str], fallback: str = "other") -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else fallback


def _error_class(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if isinstance(value, int) or normalized.isdigit():
        status = int(value)
        if status in {400, 422}:
            return "validation"
        if status in {401, 403}:
            return "authentication"
        if status in {408, 504}:
            return "timeout"
        if status == 409:
            return "conflict"
        if status == 429:
            return "rate_limit"
        if status in {502, 503}:
            return "unavailable"
        if 500 <= status <= 599:
            return "upstream"
        return "other"
    for marker, category in (
        ("timeout", "timeout"),
        ("auth", "authentication"),
        ("unauthor", "authentication"),
        ("validation", "validation"),
        ("invalid", "validation"),
        ("unavailable", "unavailable"),
        ("connection", "connection"),
        ("conflict", "conflict"),
        ("upstream", "upstream"),
        ("rate", "rate_limit"),
    ):
        if marker in normalized:
            return category
    return "other"


class SoorinMetrics:
    """Own one isolated registry so application metrics remain predictable."""

    def __init__(self, *, enabled: bool = True, registry: CollectorRegistry | None = None) -> None:
        self.enabled = enabled
        self.registry = registry or CollectorRegistry(auto_describe=True)

        self.http_requests = Counter(
            "soorin_http_requests_total",
            "HTTP requests handled by the Copilot API.",
            ("route", "method", "status_class"),
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "soorin_http_request_duration_seconds",
            "Copilot API request duration.",
            ("route", "method"),
            buckets=HTTP_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.copilot_requests = Counter(
            "soorin_copilot_requests_total",
            "Completed Copilot request workflows.",
            ("status", "workflow_mode"),
            registry=self.registry,
        )
        self.copilot_duration = Histogram(
            "soorin_copilot_request_duration_seconds",
            "End-to-end Copilot workflow duration.",
            buckets=WORKFLOW_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.workflow_stage_duration = Histogram(
            "soorin_workflow_stage_duration_seconds",
            "Duration of bounded workflow stages.",
            ("stage",),
            buckets=STAGE_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.llm_requests = Counter(
            "soorin_llm_requests_total",
            "Provider-reported LLM calls.",
            ("purpose", "provider", "model", "status"),
            registry=self.registry,
        )
        self.llm_duration = Histogram(
            "soorin_llm_request_duration_seconds",
            "LLM call duration.",
            ("purpose", "provider", "model"),
            buckets=LLM_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.llm_input_tokens = Counter(
            "soorin_llm_input_tokens_total",
            "Provider-reported LLM input tokens.",
            ("purpose", "provider", "model"),
            registry=self.registry,
        )
        self.llm_output_tokens = Counter(
            "soorin_llm_output_tokens_total",
            "Provider-reported LLM output tokens.",
            ("purpose", "provider", "model"),
            registry=self.registry,
        )
        self.tool_calls = Counter(
            "soorin_tool_calls_total",
            "Bounded capability calls.",
            ("capability", "view", "status"),
            registry=self.registry,
        )
        self.tool_duration = Histogram(
            "soorin_tool_duration_seconds",
            "Bounded capability call duration.",
            ("capability", "view"),
            buckets=DEPENDENCY_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.memory_retrieval = Counter(
            "soorin_memory_retrieval_total",
            "Memory retrieval outcomes.",
            ("kind", "result"),
            registry=self.registry,
        )
        self.memory_sufficiency = Counter(
            "soorin_memory_sufficiency_total",
            "Memory sufficiency decisions.",
            ("decision",),
            registry=self.registry,
        )
        self.memory_tool_decisions = Counter(
            "soorin_memory_tool_decisions_total",
            "Tool actions selected by memory policy.",
            ("action", "capability"),
            registry=self.registry,
        )
        self.memory_semantic_duration = Histogram(
            "soorin_memory_semantic_retrieval_duration_seconds",
            "Semantic memory retrieval duration.",
            buckets=MEMORY_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.memory_rerank_duration = Histogram(
            "soorin_memory_rerank_duration_seconds",
            "Memory reranking duration.",
            buckets=MEMORY_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.context_estimated_tokens = Gauge(
            "soorin_context_estimated_tokens",
            "Latest estimated context tokens by bounded section.",
            ("section",),
            registry=self.registry,
        )
        self.context_compacted_tokens = Gauge(
            "soorin_context_compacted_tokens",
            "Latest compacted context tokens by bounded section.",
            ("section",),
            registry=self.registry,
        )
        self.context_token_savings = Counter(
            "soorin_context_token_savings_total",
            "Estimated tokens removed by context compaction.",
            ("section",),
            registry=self.registry,
        )
        self.context_estimated_tokens_per_request = Histogram(
            "soorin_context_estimated_tokens_per_request",
            "Estimated context tokens observed for one request section.",
            ("section",),
            buckets=CONTEXT_TOKEN_BUCKETS,
            registry=self.registry,
        )
        self.context_compacted_tokens_per_request = Histogram(
            "soorin_context_compacted_tokens_per_request",
            "Compacted context tokens observed for one request section.",
            ("section",),
            buckets=CONTEXT_TOKEN_BUCKETS,
            registry=self.registry,
        )
        self.product_view_selected = Counter(
            "soorin_product_view_selected_total",
            "Bounded Product evidence view selections.",
            ("capability", "view"),
            registry=self.registry,
        )
        self.errors = Counter(
            "soorin_errors_total",
            "Coarsely classified subsystem errors.",
            ("subsystem", "error_class"),
            registry=self.registry,
        )
        self.stream_time_to_first_output = Histogram(
            "soorin_stream_time_to_first_output_seconds",
            "Time from accepted stream request to first reasoning or answer output.",
            buckets=WORKFLOW_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.stream_duration = Histogram(
            "soorin_stream_duration_seconds",
            "Total user-visible stream lifecycle duration.",
            buckets=WORKFLOW_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.stream_completions = Counter(
            "soorin_stream_completions_total",
            "Terminal streaming outcomes.",
            ("status",),
            registry=self.registry,
        )
        self.product_requests = Counter(
            "soorin_product_requests_total",
            "Product dependency request outcomes by bounded operation.",
            ("operation", "status_class"),
            registry=self.registry,
        )
        self.product_request_duration = Histogram(
            "soorin_product_request_duration_seconds",
            "Product dependency request duration by bounded operation.",
            ("operation",),
            buckets=DEPENDENCY_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.memory_lifecycle_events = Counter(
            "soorin_memory_lifecycle_events_total",
            "Canonical long-term memory lifecycle outcomes.",
            ("action", "result"),
            registry=self.registry,
        )
        self.memory_revision_conflicts = Counter(
            "soorin_memory_revision_conflicts_total",
            "Optimistic-revision conflicts at Product memory boundaries.",
            ("operation",),
            registry=self.registry,
        )
        self.memory_canonical_reload = Counter(
            "soorin_memory_canonical_reload_total",
            "Canonical memory readback outcomes after a mutation.",
            ("source", "result"),
            registry=self.registry,
        )
        self.memory_vector_operations = Counter(
            "soorin_memory_vector_operations_total",
            "Semantic memory vector operation outcomes.",
            ("operation", "result"),
            registry=self.registry,
        )
        self.workflow_fallbacks = Counter(
            "soorin_workflow_fallbacks_total",
            "Bounded deterministic workflow fallback activations.",
            ("kind",),
            registry=self.registry,
        )
        self.graph_enrichment_cycles = Counter(
            "soorin_graph_enrichment_scheduler_cycles_total",
            "Graph enrichment scheduler cycle lifecycle outcomes.",
            ("trigger", "outcome"),
            registry=self.registry,
        )
        self.graph_enrichment_assets = Counter(
            "soorin_graph_enrichment_assets_total",
            "Graph enrichment Asset outcomes.",
            ("trigger", "outcome"),
            registry=self.registry,
        )
        self.graph_enrichment_product_requests = Counter(
            "soorin_graph_enrichment_product_overview_requests_total",
            "Product detection-overview outcomes for graph enrichment.",
            ("trigger", "outcome"),
            registry=self.registry,
        )
        self.graph_enrichment_cycle_duration = Histogram(
            "soorin_graph_enrichment_scheduler_cycle_duration_seconds",
            "Duration of one bounded graph enrichment scheduler cycle.",
            ("trigger",),
            buckets=WORKFLOW_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.graph_enrichment_product_duration = Histogram(
            "soorin_graph_enrichment_product_overview_duration_seconds",
            "Product detection-overview latency for graph enrichment.",
            ("trigger",),
            buckets=DEPENDENCY_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.graph_enrichment_scheduler_running = Gauge(
            "soorin_graph_enrichment_scheduler_running",
            "Whether this process is executing an enrichment cycle.",
            registry=self.registry,
        )
        self.graph_enrichment_scheduler_owns_lease = Gauge(
            "soorin_graph_enrichment_scheduler_owns_lease",
            "Whether this process owns the distributed enrichment scheduler lease.",
            registry=self.registry,
        )
        self.graph_enrichment_backlog = Gauge(
            "soorin_graph_enrichment_backlog_assets",
            "Cached active-projection enrichment counts by bounded state.",
            ("state",),
            registry=self.registry,
        )
        self.graph_enrichment_last_run_timestamp = Gauge(
            "soorin_graph_enrichment_last_run_timestamp_seconds",
            "Unix timestamp of the latest completed enrichment cycle.",
            registry=self.registry,
        )
        self.graph_enrichment_last_success_timestamp = Gauge(
            "soorin_graph_enrichment_last_success_timestamp_seconds",
            "Unix timestamp of the latest successful enrichment cycle.",
            registry=self.registry,
        )

        for collector in self.registry._collector_to_names:
            labels = tuple(getattr(collector, "_labelnames", ()))
            unsafe = FORBIDDEN_LABEL_NAMES.intersection(labels)
            if unsafe:
                raise ValueError(f"Forbidden Prometheus labels: {', '.join(sorted(unsafe))}")

    def render(self) -> bytes:
        return generate_latest(self.registry)

    def observe_http(self, route: str, method: str, status_code: int, duration_seconds: float) -> None:
        if not self.enabled:
            return
        safe_route = route if route.startswith("/") and "?" not in route else "unmatched"
        safe_method = _bounded(method, {"get", "post", "put", "patch", "delete", "options", "head"}).upper()
        status_class = f"{max(1, min(5, int(status_code) // 100))}xx"
        self.http_requests.labels(safe_route, safe_method, status_class).inc()
        self.http_duration.labels(safe_route, safe_method).observe(max(0.0, duration_seconds))

    def observe_copilot(self, status: str, workflow_mode: str, duration_seconds: float) -> None:
        if not self.enabled:
            return
        self.copilot_requests.labels(
            _bounded(status, REQUEST_STATUSES, "failed"),
            _bounded(workflow_mode, WORKFLOW_MODES, "unknown"),
        ).inc()
        self.copilot_duration.observe(max(0.0, duration_seconds))

    def observe_stage(self, stage: str, duration_seconds: float, *, error: object = "") -> None:
        if not self.enabled:
            return
        safe_stage = _bounded(stage, BOUNDED_WORKFLOW_STAGES)
        self.workflow_stage_duration.labels(safe_stage).observe(max(0.0, duration_seconds))
        if error:
            self.observe_error("workflow", error)

    def observe_llm(self, call: object) -> None:
        if not self.enabled:
            return
        purpose = _bounded(getattr(call, "purpose", ""), BOUNDED_LLM_PURPOSES)
        provider = _bounded(getattr(call, "provider", ""), {"arvan"})
        model = str(getattr(call, "model", "") or "unknown")[:80]
        status = _bounded(
            getattr(call, "status", ""),
            {"success", "error", "partial"},
            "error",
        )
        labels = (purpose, provider, model)
        self.llm_requests.labels(*labels, status).inc()
        self.llm_duration.labels(*labels).observe(max(0, int(getattr(call, "latency_ms", 0))) / 1000)
        self.llm_input_tokens.labels(*labels).inc(max(0, int(getattr(call, "input_tokens", 0))))
        self.llm_output_tokens.labels(*labels).inc(max(0, int(getattr(call, "output_tokens", 0))))
        if status == "error":
            self.observe_error("llm", getattr(call, "http_status", "other"))

    def observe_tool(self, capability: str, views: Iterable[str], status: str, duration_seconds: float) -> None:
        if not self.enabled:
            return
        selected = tuple(dict.fromkeys(str(item).lower() for item in views if item))
        view = selected[0] if len(selected) == 1 else "multiple" if selected else "none"
        safe_view = _bounded(view, KNOWN_VIEWS)
        safe_capability = _bounded(capability, BOUNDED_CAPABILITIES)
        safe_status = _bounded(status, BOUNDED_TOOL_STATUSES)
        self.tool_calls.labels(safe_capability, safe_view, safe_status).inc()
        self.tool_duration.labels(safe_capability, safe_view).observe(max(0.0, duration_seconds))
        if safe_status in {"unavailable", "invalid"}:
            self.observe_error("tool", safe_status)

    def observe_memory_retrieval(self, kind: str, result: str, duration_seconds: float | None = None) -> None:
        if not self.enabled:
            return
        safe_kind = _bounded(kind, MEMORY_KINDS)
        safe_result = _bounded(result, MEMORY_RESULTS)
        self.memory_retrieval.labels(safe_kind, safe_result).inc()
        if duration_seconds is not None:
            if safe_kind == "semantic":
                self.memory_semantic_duration.observe(max(0.0, duration_seconds))

    def observe_rerank(self, duration_seconds: float) -> None:
        if self.enabled:
            self.memory_rerank_duration.observe(max(0.0, duration_seconds))

    def observe_memory_decision(self, decision: str) -> None:
        if not self.enabled:
            return
        self.memory_sufficiency.labels(_bounded(decision, MEMORY_DECISIONS, "live")).inc()

    def observe_memory_action(self, action: str, capability: str) -> None:
        if not self.enabled:
            return
        self.memory_tool_decisions.labels(
            _bounded(action, MEMORY_ACTIONS, "live"),
            _bounded(capability, BOUNDED_CAPABILITIES),
        ).inc()

    def observe_memory_policy(self, decision: str, action: str, capability: str) -> None:
        self.observe_memory_decision(decision)
        self.observe_memory_action(action, capability)

    def observe_context(self, section: str, estimated: int, compacted: int) -> None:
        if not self.enabled:
            return
        safe_section = _bounded(section, CONTEXT_SECTIONS)
        estimated = max(0, int(estimated))
        compacted = max(0, int(compacted))
        self.context_estimated_tokens.labels(safe_section).set(estimated)
        self.context_compacted_tokens.labels(safe_section).set(compacted)
        self.context_estimated_tokens_per_request.labels(safe_section).observe(estimated)
        self.context_compacted_tokens_per_request.labels(safe_section).observe(compacted)
        self.context_token_savings.labels(safe_section).inc(max(0, estimated - compacted))

    def observe_stream(
        self,
        *,
        duration_seconds: float,
        status: str,
        first_output_seconds: float | None = None,
    ) -> None:
        if not self.enabled:
            return
        if first_output_seconds is not None:
            self.stream_time_to_first_output.observe(max(0.0, first_output_seconds))
        self.stream_duration.observe(max(0.0, duration_seconds))
        self.stream_completions.labels(_bounded(status, STREAM_STATUSES, "error")).inc()

    def observe_product(
        self,
        operation: str,
        *,
        duration_seconds: float,
        status_code: int | None = None,
    ) -> None:
        if not self.enabled:
            return
        safe_operation = _bounded(operation, PRODUCT_OPERATIONS)
        if status_code is None:
            status_class = "exception"
        else:
            status_class = f"{max(2, min(5, int(status_code) // 100))}xx"
            status_class = _bounded(status_class, PRODUCT_STATUS_CLASSES, "exception")
        self.product_requests.labels(safe_operation, status_class).inc()
        self.product_request_duration.labels(safe_operation).observe(max(0.0, duration_seconds))
        if status_class != "2xx":
            self.observe_error("product", status_code if status_code is not None else "connection")

    def observe_memory_lifecycle(self, action: str, result: str = "success") -> None:
        if self.enabled:
            self.memory_lifecycle_events.labels(
                _bounded(action, MEMORY_LIFECYCLE_ACTIONS),
                _bounded(result, OBSERVATION_RESULTS, "failure"),
            ).inc()

    def observe_memory_revision_conflict(self, operation: str) -> None:
        if self.enabled:
            self.memory_revision_conflicts.labels(
                _bounded(operation, MEMORY_REVISION_OPERATIONS)
            ).inc()

    def observe_memory_canonical_reload(self, source: str, result: str) -> None:
        if self.enabled:
            self.memory_canonical_reload.labels(
                _bounded(source, MEMORY_CANONICAL_SOURCES),
                _bounded(result, OBSERVATION_RESULTS, "failure"),
            ).inc()

    def observe_memory_vector(self, operation: str, result: str) -> None:
        if self.enabled:
            self.memory_vector_operations.labels(
                _bounded(operation, MEMORY_VECTOR_OPERATIONS),
                _bounded(result, OBSERVATION_RESULTS, "failure"),
            ).inc()

    def observe_workflow_fallback(self, kind: str) -> None:
        if self.enabled:
            self.workflow_fallbacks.labels(_bounded(kind, WORKFLOW_FALLBACK_KINDS)).inc()

    def graph_enrichment_cycle_started(self, trigger: str) -> None:
        if not self.enabled:
            return
        safe_trigger = _bounded(trigger, GRAPH_ENRICHMENT_TRIGGERS)
        self.graph_enrichment_cycles.labels(safe_trigger, "started").inc()
        self.graph_enrichment_scheduler_running.set(1)

    def observe_graph_enrichment_cycle(
        self,
        trigger: str,
        *,
        outcome: str,
        duration_seconds: float,
        timestamp: float | None = None,
    ) -> None:
        if not self.enabled:
            return
        safe_trigger = _bounded(trigger, GRAPH_ENRICHMENT_TRIGGERS)
        safe_outcome = _bounded(outcome, {"completed", "failed"}, "failed")
        self.graph_enrichment_cycles.labels(safe_trigger, safe_outcome).inc()
        self.graph_enrichment_cycle_duration.labels(safe_trigger).observe(
            max(0.0, duration_seconds)
        )
        completed_at = time.time() if timestamp is None else max(0.0, timestamp)
        self.graph_enrichment_last_run_timestamp.set(completed_at)
        if safe_outcome == "completed":
            self.graph_enrichment_last_success_timestamp.set(completed_at)
        self.graph_enrichment_scheduler_running.set(0)

    def observe_graph_enrichment_assets(
        self,
        trigger: str,
        *,
        attempted: int = 0,
        succeeded: int = 0,
        failed: int = 0,
        unavailable: int = 0,
        updated: int = 0,
        skipped: int = 0,
    ) -> None:
        if not self.enabled:
            return
        safe_trigger = _bounded(trigger, GRAPH_ENRICHMENT_TRIGGERS)
        values = {
            "attempted": attempted,
            "succeeded": succeeded,
            "failed": failed,
            "unavailable": unavailable,
            "updated": updated,
            "skipped": skipped,
        }
        for outcome, value in values.items():
            if int(value) > 0:
                self.graph_enrichment_assets.labels(safe_trigger, outcome).inc(
                    int(value)
                )

    def observe_graph_enrichment_product(
        self,
        trigger: str,
        *,
        outcome: str,
        duration_seconds: float,
    ) -> None:
        if not self.enabled:
            return
        safe_trigger = _bounded(trigger, GRAPH_ENRICHMENT_TRIGGERS)
        safe_outcome = _bounded(
            outcome,
            GRAPH_ENRICHMENT_PRODUCT_OUTCOMES,
            "error",
        )
        self.graph_enrichment_product_requests.labels(
            safe_trigger, safe_outcome
        ).inc()
        self.graph_enrichment_product_duration.labels(safe_trigger).observe(
            max(0.0, duration_seconds)
        )

    def set_graph_enrichment_lease_owned(self, owned: bool) -> None:
        if self.enabled:
            self.graph_enrichment_scheduler_owns_lease.set(1 if owned else 0)

    def set_graph_enrichment_backlog(self, counts: dict[str, int]) -> None:
        if not self.enabled:
            return
        for state in GRAPH_ENRICHMENT_BACKLOG_STATES:
            self.graph_enrichment_backlog.labels(state).set(
                max(0, int(counts.get(state, 0)))
            )

    def observe_view(self, capability: str, views: Iterable[str]) -> None:
        if not self.enabled:
            return
        for view in tuple(dict.fromkeys(str(item).lower() for item in views if item)):
            self.product_view_selected.labels(
                _bounded(capability, BOUNDED_CAPABILITIES),
                _bounded(view, KNOWN_VIEWS),
            ).inc()

    def observe_error(self, subsystem: str, error: object) -> None:
        if self.enabled:
            self.errors.labels(
                _bounded(subsystem, BOUNDED_SUBSYSTEMS),
                _error_class(error),
            ).inc()


BOUNDED_CAPABILITIES = frozenset(
    {
        "asset.get_profile",
        "asset.get_detection",
        "graph.get_summary",
        "graph.get_neighbors",
        "graph.get_relationship",
        "graph.compare_assets",
        "graph.find_path",
        "knowledge.search",
    }
)
BOUNDED_TOOL_STATUSES = frozenset(
    {"ok", "empty", "not_found", "partial", "unavailable", "invalid", "not_configured"}
)
BOUNDED_LLM_PURPOSES = frozenset(
    {"intent_router", "intent_router_repair", "planner", "planner_repair", "chat"}
)
BOUNDED_SUBSYSTEMS = frozenset(
    {"http", "workflow", "llm", "tool", "memory", "context", "graph", "product", "rag"}
)
BOUNDED_WORKFLOW_STAGES = frozenset(
    {
        "resolve_entities",
        "route",
        "validate_task",
        "build_direct_plan",
        "build_plan",
        "validate_plan",
        "build_fallback_plan",
        "dispatch_specialists",
        "join_specialist_results",
        "build_evidence",
        "review_retrieval",
        "supplemental_retrieval",
        "compose_context",
        "review_context",
        "synthesize",
        "update_memory",
        "clarification",
        "clarification_response",
        "safe_failure",
        "safe_failure_response",
    }
)
GRAPH_ENRICHMENT_TRIGGERS = frozenset({"scheduled", "manual", "on_demand"})
GRAPH_ENRICHMENT_PRODUCT_OUTCOMES = frozenset({"success", "error", "unavailable"})
GRAPH_ENRICHMENT_BACKLOG_STATES = frozenset(
    {"pending", "stale", "error", "unavailable", "backlog"}
)


_metrics_lock = Lock()
_metrics: SoorinMetrics | None = None


def configure_metrics(enabled: bool) -> SoorinMetrics:
    global _metrics
    with _metrics_lock:
        if _metrics is None:
            _metrics = SoorinMetrics(enabled=enabled)
        else:
            _metrics.enabled = enabled
        return _metrics


def get_metrics() -> SoorinMetrics:
    global _metrics
    if _metrics is None:
        return configure_metrics(False)
    return _metrics


class PrometheusASGIMiddleware:
    """Observe HTTP lifecycle without buffering normal or streaming responses."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_code = 500

        async def observe_send(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status") or 500)
            await send(message)

        try:
            await self.app(scope, receive, observe_send)
        except Exception as exc:
            get_metrics().observe_error("http", type(exc).__name__)
            raise
        finally:
            route = scope.get("route")
            route_path = str(getattr(route, "path", "unmatched"))
            get_metrics().observe_http(
                route_path,
                str(scope.get("method") or ""),
                status_code,
                time.perf_counter() - started,
            )
