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

Context and errors:

- `soorin_context_estimated_tokens`
- `soorin_context_compacted_tokens`
- `soorin_context_token_savings_total`
- `soorin_errors_total`

Metrics are recorded at existing authoritative lifecycle boundaries. No Python
code parses log text to reconstruct business events.

## Cardinality And Data Safety

Prometheus labels are restricted to bounded routes, methods, status classes,
workflow modes, stages, configured providers/models, registered capabilities,
views, memory decisions, context sections, and coarse error classes.

Never add these as metric labels or Loki stream labels:

```text
request_id, trace_id, user_id, session_id, conversation_id, memory_id,
IP address, hostname, entity ID, arbitrary errors, prompts, model output
```

Existing request and trace IDs remain searchable inside safe application log
content, not indexed labels. Alloy adds only `service_name`, `compose_service`,
and `environment`. It does not add credentials, Product JWTs, `x-hwid`, prompts,
Product payloads, or Evidence Snapshot content.

The Docker socket is mounted into Alloy read-only. Docker socket access is still
privileged host metadata access; keep the Alloy container trusted and retain the
strict Compose project/service filters.

## Dashboard

The provisioned dashboard contains restrained sections for:

1. System health, traffic, error rate, and p50/p95 latency.
2. LLM token usage, call outcomes, model/purpose breakdown, and latency.
3. Direct/planned workflow mix, stages, capabilities, and errors.
4. Memory hit/miss, reuse/verify/live policy, and retrieval latency.
5. Context size, compaction savings, and Product view selection.
6. API/LLM errors, memory/tool decisions, and context-compaction logs.

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
