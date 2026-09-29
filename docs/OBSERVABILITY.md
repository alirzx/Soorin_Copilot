# Soorin Copilot Observability

## Architecture

The optional local observability path is:

```text
Copilot API -- authenticated /metrics --> Prometheus --+
                                                       +--> Grafana
API/UI Docker logs --> Grafana Alloy --> Loki --------+
```

- **Prometheus** stores bounded numeric time series.
- **Loki** stores existing application/container log lines.
- **Grafana Alloy** discovers only the `api` and `ui` services in the
  `soorin-copilot` Compose project and forwards their logs to Loki.
- **Grafana** provisions both data sources and the
  **Soorin Copilot — Operations & Agent Workflow** dashboard automatically.

This does not replace structured logs, Human Trace, or Evidence Snapshots.
Human Trace remains a bounded explanation of one workflow. Evidence Snapshots
remain an explicitly enabled diagnostic artifact. Neither is converted into
Prometheus labels or automatically indexed as a separate telemetry stream.

## Configuration

Application settings:

```env
SOORIN_METRICS_ENABLED=true
SOORIN_METRICS_PATH=/metrics
```

Profile settings use `SOORIN_OBSERVABILITY_*` variables from `.env.example`.
Grafana, Prometheus, and Loki bind to `127.0.0.1` by default. Change the Grafana
admin placeholder before shared/server use.

Set `SOORIN_LOG_FORMAT=json` for production-style collection. Alloy parses the
JSON fields while retaining non-JSON console lines through a fallback path.
`SOORIN_OBSERVABILITY_ENVIRONMENT` is attached both to scraped API series and
to collected log streams, so the Grafana environment variable filters both
data sources consistently.

Prometheus environment expansion applies only to external labels. The Compose
entrypoint therefore validates the environment name and renders the checked-in
Prometheus template to `/tmp/prometheus.yml` before starting the same Prometheus
binary. This is an in-container configuration render; it adds no service,
volume, port, or external dependency.

`/metrics` intentionally preserves Copilot API authentication because the API
itself may be bound beyond localhost. Prometheus receives
`SOORIN_COPILOT_API_KEY` through a read-only Compose secret and scrapes
`http://api:6998/metrics` over the private Compose network. The endpoint never
calls an LLM, Product API, graph, or Qdrant.

The checked-in Prometheus configuration uses `/metrics`. If
`SOORIN_METRICS_PATH` is changed, update the matching `metrics_path` in
`observability/prometheus/prometheus.yml` before deployment.

## Start And Stop

Normal Copilot startup remains unchanged and does not start observability:

```bash
docker compose up -d
```

Start Copilot with the optional stack:

```bash
docker compose --profile observability up -d
```

Default local addresses:

- Grafana: `http://127.0.0.1:3000`
- Prometheus: `http://127.0.0.1:9090`
- Loki: `http://127.0.0.1:3100`

Stop without deleting persistent data:

```bash
docker compose --profile observability down
```

Disable application metrics with `SOORIN_METRICS_ENABLED=false`. The core API
and UI continue to operate; Prometheus will report its target unavailable.

## Metric Catalog

HTTP:

- `soorin_http_requests_total`
- `soorin_http_request_duration_seconds`

Copilot and workflow:

- `soorin_copilot_requests_total`
- `soorin_copilot_request_duration_seconds`
- `soorin_workflow_stage_duration_seconds`
- `soorin_stream_time_to_first_output_seconds`
- `soorin_stream_duration_seconds`
- `soorin_stream_completions_total`
- `soorin_workflow_fallbacks_total`

Bounded adaptive Investigator:

- `soorin_agent_loops_total{status}`
- `soorin_agent_turns_per_request`
- `soorin_agent_loop_duration_seconds{status}`
- `soorin_agent_actions_total{result}`
- `soorin_agent_stop_reasons_total{reason}`
- `soorin_agent_budget_exhaustion_total{budget_type}`
- `soorin_investigator_context_tokens`

LLM:

- `soorin_llm_requests_total`
- `soorin_llm_request_duration_seconds`
- `soorin_llm_input_tokens_total`
- `soorin_llm_output_tokens_total`

Tools and views:

- `soorin_tool_calls_total`
- `soorin_tool_duration_seconds`
- `soorin_product_view_selected_total`

Memory:

- `soorin_memory_retrieval_total`
- `soorin_memory_sufficiency_total`
- `soorin_memory_tool_decisions_total`
- `soorin_memory_semantic_retrieval_duration_seconds`
- `soorin_memory_rerank_duration_seconds`
- `soorin_memory_lifecycle_events_total`
- `soorin_memory_revision_conflicts_total`
- `soorin_memory_canonical_reload_total`
- `soorin_memory_vector_operations_total`

Product dependency:

- `soorin_product_requests_total`
- `soorin_product_request_duration_seconds`

Context and errors:

- `soorin_context_estimated_tokens`
- `soorin_context_compacted_tokens`
- `soorin_context_estimated_tokens_per_request`
- `soorin_context_compacted_tokens_per_request`
- `soorin_context_token_savings_total`
- `soorin_errors_total`

Bounded adaptive execution:

- `soorin_agent_loops_total`
- `soorin_agent_turns_per_request`
- `soorin_agent_loop_duration_seconds`
- `soorin_agent_actions_total`
- `soorin_agent_stop_reasons_total`
- `soorin_agent_budget_exhaustion_total`
- `soorin_agent_equivalent_actions_blocked_total{reason}`
- `soorin_agent_evidence_references_total{change}`
- `soorin_agent_material_progress_turns_total{progress}`
- `soorin_evidence_review_outcomes_total{mode,outcome}`
- `soorin_investigator_context_tokens`
- `soorin_investigator_context_tokens_before_compaction`
- `soorin_investigator_context_token_savings_total`

Adaptive labels are closed low-cardinality sets. They never contain request,
session, entity, gap, query, or reference identifiers.

Metrics are recorded at existing authoritative lifecycle boundaries. No Python
code parses log text to reconstruct business events.

All latency and context-size histograms use explicit workload-specific buckets.
The upper bounds cover long Copilot/LLM/dependency workflows and large context
windows; they are not Prometheus client defaults. Counter panels use
`$__rate_interval` for rates and `increase(...[$__range])` for selected-range
totals. Token throughput is displayed as tokens/minute, while separate panels
show selected-range totals, tokens per LLM call, and tokens per completed
Copilot request.

Router and Planner repair calls and Investigator decisions are real provider
calls. They are included in both Prometheus LLM totals and the Product
usage-report aggregate under their own purposes, including
`purpose=investigator`. This keeps operational telemetry and Product
accounting aligned with actual provider usage.

## Adaptive Events and Human Trace

The adaptive path emits allow-listed workflow events at its authoritative
boundaries:

- `orchestration_mode_selected`
- `agent_loop_initialized`
- `agent_progress_evaluated`
- `agent_decision_requested`, `agent_decision_received`, and `agent_decision_invalid`
- `agent_context_budget_checked` and `agent_context_compacted`
- `agent_action_validated` and `agent_action_rejected`
- `agent_evidence_equivalent` and `agent_action_equivalent_blocked`
- `agent_tool_observation`
- `agent_evidence_reference_created`
- `agent_observation_delta_built` and `agent_ledger_updated`
- `agent_no_material_progress`
- `agent_loop_finished`

Safe fields include bounded orchestration mode, turn/LLM/tool counts, decision
kind, gap/reference change counts, stop reason, material-progress flag,
pre/post context-token estimates, compacted-token count, and budget category.
Prompts, assessment text, model output, raw provider payloads, credentials, and
entity values are not emitted as event fields or metric labels.

Human Trace uses the existing trace renderer. It records orchestration mode in
`TASK AND PLAN` and, only when adaptive execution runs, adds an
`ADAPTIVE AGENT LOOP` section with loop status, stop reason, aggregate counts,
and up to eight per-turn reference/gap deltas, skipped-execution reasons,
context-token estimates, and capability/status/progress summaries. Summary-mode
trace shows the mode and the aggregate turn/stop outcome. This is a bounded
workflow explanation, not chain-of-thought.

## Cardinality And Data Safety

Prometheus labels are restricted to bounded routes, methods, status classes,
workflow modes, stages, configured providers/models, registered capabilities,
views, memory decisions, context sections, and coarse error classes.

Never add these as metric labels or Loki stream labels:

```text
request_id, trace_id, user_id, session_id, conversation_id, memory_id,
IP address, hostname, entity ID, arbitrary errors, prompts, model output
```

Existing request and trace IDs remain searchable as structured metadata or
inside safe application log content, never as indexed Loki stream labels.
Alloy indexes only bounded fields: `service_name`, `compose_service`,
`environment`, and parsed log `level`. It parses `event`, `request_id`,
`trace_id`, `status`, and `error_type` for query-time use without increasing
stream cardinality. It does not add credentials, Product JWTs, `x-hwid`,
prompts, Product payloads, or Evidence Snapshot content. The JSON formatter
adds a bounded, redacted exception diagnostic when `exc_info` is present.

The Docker socket is mounted into Alloy read-only. Docker socket access is still
privileged host metadata access; keep the Alloy container trusted and retain the
strict Compose project/service filters.

## Dashboard

The provisioned dashboard contains restrained sections for:

1. RED service health, traffic, HTTP outcomes, latency, and stream experience.
2. Agent workflow mode, completion, end-to-end latency, calls, and fallbacks.
3. LLM throughput, totals, per-call/per-request cost proxies, and latency.
4. Memory policy, retrieval, lifecycle, revision, canonical reload, and index.
5. Context gauges, per-request distributions, savings, and compaction ratio.
6. Product dependency volume, error ratio, latency, and range failures.
7. Structured application, dependency, memory, and context diagnostic logs.

Dashboard variables use only low-cardinality environment, provider, model,
capability, and view labels.

## Storage And Deployment Boundaries

Prometheus, Loki, and Grafana use dedicated named volumes. The default retention
is seven days (`168h`) and is configurable. No observability data is written into
the Copilot application directory.

This is a single-node local/server-validation design, not a distributed Loki or
multi-tenant monitoring service. A future customer deployment must explicitly
decide customer consent, data residency, tenant isolation, retention, TLS/auth,
outbound connectivity, redaction, and whether telemetry may leave the customer
network. The current design exports nothing remotely.

No alert thresholds or SLO recording rules are checked in because the project
does not yet define production SLOs. Tempo/OpenTelemetry tracing, host/container
resource exporters, and HA/object-storage backends remain explicit deployment
follow-ups rather than implicit parts of this local topology.

## Applying Configuration Changes

The checked-in files are provisioned at container startup. After changing this
observability configuration in a deployed environment:

- restart/recreate the API to load Python instrumentation changes;
- reload or restart Prometheus to load scrape labels/configuration;
- restart Alloy to load its parsing pipeline;
- allow Grafana provisioning to rescan the dashboard, or restart Grafana.

Loki configuration is unchanged by this hardening pass. Validate changes in a
maintenance window; this repository task intentionally does not restart the
running stack.

## Troubleshooting

- Prometheus target `401`: verify the Compose secret is sourced from the same
  `SOORIN_COPILOT_API_KEY` used by the API.
- Prometheus target `404`: verify both configured metrics paths are `/metrics`.
- No logs: verify Alloy can read `/var/run/docker.sock` and that the Compose
  project is named `soorin-copilot` with services `api` and `ui`.
- Empty dashboard after startup: allow at least two scrape intervals and confirm
  the Prometheus target is up.
- Remove the optional stack and volumes only when historical telemetry is no
  longer required: `docker compose --profile observability down -v`.
