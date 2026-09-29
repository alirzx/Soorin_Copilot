# Soorin Copilot Environment Variables

The repository-root `.env` is the private runtime configuration for Soorin Copilot. `.env.example` is the tracked, secret-free schema and is the authoritative key list for the deployed code revision.

Do not commit `.env`. Secrets such as API keys, passwords, Product credentials, HWIDs, captcha bypass values, and Grafana credentials must remain private.

This document explains the current variable groups and deployment semantics. For the exact current key set and safe defaults, use `.env.example` from the same Git revision.

## Configuration precedence

Application settings are resolved from process environment and the repository-root dotenv configuration. Container-specific paths are overridden by Compose where necessary.

For deployments, keep one root `.env` aligned with the checked-out code. Machine-specific filesystem paths and secrets should differ between developer and server environments while behavioral/runtime settings should be synchronized deliberately.

## Deployment and host mounts

### `SOORIN_IMAGE_TAG`

Selects the Docker image tag used by the Makefile and Compose stack.

### `SOORIN_RESTART_POLICY`

Docker restart policy for long-running services.

### `SOORIN_HF_CACHE_HOST_PATH`

Host path containing the Hugging Face cache mounted read-only into the API container.

Examples:

```env
# developer workstation
SOORIN_HF_CACHE_HOST_PATH=/home/user/.cache/huggingface

# server
SOORIN_HF_CACHE_HOST_PATH=/srv/soorin-copilot/huggingface
```

The Makefile and Compose deployment use the same value. Absolute paths are used directly. Relative paths resolve from the repository root. The Makefile falls back to `./huggingface` only when the variable is absent.

### API/UI host publishing

- `SOORIN_API_BIND_IP`
- `SOORIN_API_HOST_PORT`
- `SOORIN_UI_BIND_IP`
- `SOORIN_UI_HOST_PORT`

These control host-side Docker port publishing. They are separate from the internal API and Streamlit ports.

## API and UI runtime

- `API_HOST`
- `API_PORT`
- `API_RELOAD`
- `STREAMLIT_SERVER_PORT`
- `SOORIN_API_BASE_URL`
- `SOORIN_API_TIMEOUT_SECONDS`
- `SOORIN_COPILOT_API_KEY`

`API_RELOAD=false` is appropriate for container/server execution. The UI uses a container override pointing to `http://api:6998` when running in Compose.

`SOORIN_COPILOT_API_KEY` protects non-public Copilot API routes. The application supports the dedicated Copilot API-key header and compatible Bearer authentication. Keep the key secret.

## Logging

- `LOG_LEVEL`
- `SOORIN_LOG_FORMAT`
- `SOORIN_LOG_COLOR`
- `SOORIN_LOG_FILE_ENABLED`
- `SOORIN_LOG_FILE_LEVEL`
- `SOORIN_LOG_FILE_PATH`
- `SOORIN_LOG_FILE_MAX_BYTES`
- `SOORIN_LOG_FILE_BACKUP_COUNT`

`SOORIN_LOG_FORMAT` supports human-readable console and JSON operation. Container deployment overrides the file path into `/workspace/data/runtime/logs/`.

## Metrics and observability

- `SOORIN_METRICS_ENABLED`
- `SOORIN_METRICS_PATH`
- `SOORIN_OBSERVABILITY_GRAFANA_BIND_IP`
- `SOORIN_OBSERVABILITY_GRAFANA_PORT`
- `SOORIN_OBSERVABILITY_PROMETHEUS_BIND_IP`
- `SOORIN_OBSERVABILITY_PROMETHEUS_PORT`
- `SOORIN_OBSERVABILITY_LOKI_BIND_IP`
- `SOORIN_OBSERVABILITY_LOKI_PORT`
- `SOORIN_OBSERVABILITY_RETENTION`
- `SOORIN_OBSERVABILITY_ENVIRONMENT`
- `SOORIN_GRAFANA_ADMIN_USER`
- `SOORIN_GRAFANA_ADMIN_PASSWORD`

The metrics endpoint remains authenticated. Prometheus receives the configured Copilot API key through the deployment-generated metrics secret.

## LLM transport

### Shared provider settings

- `SOORIN_LLM_ENABLED`
- `SOORIN_LLM_PROVIDER`
- `SOORIN_LLM_AUTH_SCHEME`
- `SOORIN_LLM_CHAT_PATH`
- `SOORIN_LLM_CONNECT_TIMEOUT_SECONDS`
- `SOORIN_LLM_MAX_TRANSIENT_RETRIES`
- `SOORIN_LLM_RETRY_BASE_DELAY_SECONDS`
- `SOORIN_LLM_RETRY_MAX_DELAY_SECONDS`
- `SOORIN_LLM_EXPOSE_REASONING`
- `SOORIN_LLM_LOG_RAW_RESPONSE`

Supported provider values are:

```text
arvan
vllm
ollama
openai_compatible
```

Arvan requires the configured role API keys. Private compatible endpoints may omit an API key. `SOORIN_LLM_PROVIDER` is currently global, while each role has its own deployment URL/model/limits.

### Context budgeting

- `SOORIN_LLM_CONTEXT_WINDOW_TOKENS`
- `SOORIN_LLM_RESERVED_OUTPUT_TOKENS`
- `SOORIN_LLM_CONTEXT_SAFETY_MARGIN_TOKENS`
- `SOORIN_LLM_TOKEN_ESTIMATE_MULTIPLIER`

These values define the global model-context contract used by the Context Composer. They must not exceed the real serving model's context capabilities.

### Router

- `SOORIN_ROUTER_BASE_URL`
- `SOORIN_ROUTER_MODEL`
- `SOORIN_ROUTER_API_KEY`
- `SOORIN_ROUTER_TIMEOUT_SECONDS`
- `SOORIN_ROUTER_MAX_TOKENS`
- `SOORIN_ROUTER_RETRY_MAX_TOKENS`
- `SOORIN_ROUTER_TEMPERATURE`
- `SOORIN_ROUTER_TOP_P`
- `SOORIN_ROUTER_SUPPORTS_TEMPERATURE`
- `SOORIN_ROUTER_SUPPORTS_TOP_P`

Router behavior:

- `SOORIN_INTENT_ROUTER_ENABLED`
- `SOORIN_INTENT_ROUTER_SYSTEM_PROMPT_PATH`
- `SOORIN_INTENT_ROUTER_RETRY_ENABLED`

### Planner

- `SOORIN_PLANNER_BASE_URL`
- `SOORIN_PLANNER_MODEL`
- `SOORIN_PLANNER_API_KEY`
- `SOORIN_PLANNER_TIMEOUT_SECONDS`
- `SOORIN_PLANNER_MAX_TOKENS`
- `SOORIN_PLANNER_RETRY_MAX_TOKENS`
- `SOORIN_PLANNER_TEMPERATURE`
- `SOORIN_PLANNER_TOP_P`
- `SOORIN_PLANNER_SUPPORTS_TEMPERATURE`
- `SOORIN_PLANNER_SUPPORTS_TOP_P`
- `SOORIN_PLANNER_ENABLED`
- `SOORIN_PLANNER_REPAIR_ENABLED`
- `SOORIN_PLANNER_SYSTEM_PROMPT_PATH`

### Investigator and adaptive orchestration

The Investigator is a separate OpenAI-compatible role used only when adaptive orchestration is enabled. Its deployment is not implicitly aliased to Router, Planner, or Synthesizer; configure the same endpoint/model explicitly if desired.

- `SOORIN_INVESTIGATOR_BASE_URL` — no default; required when adaptive mode is enabled.
- `SOORIN_INVESTIGATOR_MODEL` — `CHANGE_ME_MODEL`.
- `SOORIN_INVESTIGATOR_API_KEY` — empty by default; provider policy may require it.
- `SOORIN_INVESTIGATOR_TIMEOUT_SECONDS` — `60`.
- `SOORIN_INVESTIGATOR_MAX_TOKENS` — `512`.
- `SOORIN_INVESTIGATOR_TEMPERATURE` — unset in application settings; `.env.example` uses `0.0`.
- `SOORIN_INVESTIGATOR_TOP_P` — unset in application settings; `.env.example` uses `0.1`.
- `SOORIN_INVESTIGATOR_SUPPORTS_TEMPERATURE` — `false`.
- `SOORIN_INVESTIGATOR_SUPPORTS_TOP_P` — `false`.
- `SOORIN_INVESTIGATOR_SYSTEM_PROMPT_PATH` — `app/prompts/investigator_system_prompt.md`.
- `SOORIN_INVESTIGATOR_MAX_INPUT_TOKENS` — `8000`, the deterministic compaction target.
- `SOORIN_INVESTIGATOR_HARD_INPUT_TOKENS` — `12000`, the fail-closed hard limit; it must be at least the target.

Adaptive selection and request-level budgets:

- `SOORIN_ADAPTIVE_AGENT_ENABLED` — `false`; this preserves direct/fixed behavior by default.
- `SOORIN_AGENT_MAX_INVESTIGATOR_TURNS` — `4`.
- `SOORIN_AGENT_MAX_LLM_CALLS` — `6`, including Router, Investigator, and final Synthesizer calls. It must leave two non-Investigator calls reserved.
- `SOORIN_AGENT_MAX_TOTAL_CAPABILITY_CALLS` — `6` across all adaptive turns.
- `SOORIN_AGENT_MAX_DEEPENED_ENTITIES` — `2`.
- `SOORIN_AGENT_MAX_TECHNICAL_FAILURES` — `2`.

Existing agent-wide bounds continue to apply: `SOORIN_AGENT_MAX_CAPABILITY_CALLS`, `SOORIN_AGENT_MAX_ENTITIES`, `SOORIN_AGENT_MAX_GRAPH_DEPTH`, `SOORIN_AGENT_EXECUTOR_MAX_CONCURRENCY`, and `SOORIN_AGENT_REQUEST_TIMEOUT_SECONDS`. The checked-in example uses a 90-second request timeout. LangGraph recursion is a backstop and is not the adaptive budget.

### Synthesizer

- `SOORIN_SYNTHESIZER_BASE_URL`
- `SOORIN_SYNTHESIZER_MODEL`
- `SOORIN_SYNTHESIZER_API_KEY`
- `SOORIN_SYNTHESIZER_TIMEOUT_SECONDS`
- `SOORIN_SYNTHESIZER_MAX_TOKENS`
- `SOORIN_SYNTHESIZER_BRIEF_OUTPUT_TOKENS`
- `SOORIN_SYNTHESIZER_STANDARD_OUTPUT_TOKENS`
- `SOORIN_SYNTHESIZER_DEEP_OUTPUT_TOKENS`
- `SOORIN_SYNTHESIZER_RETRY_MAX_TOKENS`
- `SOORIN_SYNTHESIZER_TEMPERATURE`
- `SOORIN_SYNTHESIZER_TOP_P`
- `SOORIN_SYNTHESIZER_SUPPORTS_TEMPERATURE`
- `SOORIN_SYNTHESIZER_SUPPORTS_TOP_P`
- `SOORIN_SYSTEM_PROMPT_PATH`

## Agent execution limits

- `SOORIN_AGENT_MAX_SUPPLEMENTAL_RETRIEVALS`
- `SOORIN_AGENT_MAX_CAPABILITY_CALLS`
- `SOORIN_AGENT_MAX_ENTITIES`
- `SOORIN_AGENT_MAX_GRAPH_DEPTH`
- `SOORIN_AGENT_EXECUTOR_MAX_CONCURRENCY`
- `SOORIN_AGENT_REQUEST_TIMEOUT_SECONDS`

These are application-enforced safety/performance bounds around capability execution.

## Product Backend

### Base connection and authentication

- `SOORIN_PRODUCT_API_BASE_URL`
- `SOORIN_PRODUCT_LOGIN_PATH`
- `SOORIN_PRODUCT_API_TOKEN`
- `SOORIN_PRODUCT_USERNAME`
- `SOORIN_PRODUCT_PASSWORD`
- `SOORIN_PRODUCT_CAPTCHA_BYPASS`
- `SOORIN_PRODUCT_TOKEN_REFRESH_SECONDS`
- `SOORIN_PRODUCT_HWID`
- `SOORIN_PRODUCT_CONNECT_TIMEOUT_SECONDS`
- `SOORIN_PRODUCT_READ_TIMEOUT_SECONDS`
- `SOORIN_PRODUCT_MAX_RETRIES`
- `SOORIN_PRODUCT_RETRY_BACKOFF_SECONDS`

### Product evidence paths

- `SOORIN_PRODUCT_TOPOLOGY_PATH`
- `SOORIN_PRODUCT_ASSET_PROFILE_PATH`
- `SOORIN_PRODUCT_ASSET_DETECTION_PATH`
- `SOORIN_PRODUCT_ASSET_DETECTION_OVERVIEW_PATH`
- `SOORIN_PRODUCT_ASSET_DETECTION_EVIDENCE_PATH`
- `SOORIN_PRODUCT_ASSET_DETECTION_SIMILARITY_PATH`
- `SOORIN_PRODUCT_ASSET_DETECTION_CLUSTER_PATH`

### Product memory/chat paths

- `SOORIN_PRODUCT_MEMORY_THREAD_STATE_PATH`
- `SOORIN_PRODUCT_MEMORY_LTM_PATH`
- `SOORIN_PRODUCT_CHAT_ROOMS_PATH`

### Usage reporting

- `LLM_USAGE_REPORTING_ENABLED`
- `LLM_USAGE_REPORTING_URL`

### Detection cache

- `SOORIN_DETECTION_CACHE_ENABLED`
- `SOORIN_DETECTION_CACHE_TTL_SECONDS`
- `SOORIN_DETECTION_STALE_ON_ERROR`

## Neo4j

- `SOORIN_NEO4J_URI`
- `SOORIN_NEO4J_USER`
- `SOORIN_NEO4J_PASSWORD`
- `SOORIN_NEO4J_DATABASE`
- `SOORIN_NEO4J_BOLT_BIND_IP`
- `SOORIN_NEO4J_BOLT_HOST_PORT`
- `SOORIN_NEO4J_QUERY_TIMEOUT_SECONDS`
- `SOORIN_NEO4J_SYNC_BATCH_SIZE`
- `SOORIN_NEO4J_MAX_CONNECTION_POOL_SIZE`

Inside Compose the API uses `bolt://neo4j:7687`. Host Bolt binding is independently configurable and should normally remain private.

## Graph enrichment

- `SOORIN_GRAPH_ENRICHMENT_ENABLED`
- `SOORIN_GRAPH_ENRICHMENT_CONCURRENCY`
- `SOORIN_GRAPH_ENRICHMENT_BATCH_SIZE`
- `SOORIN_GRAPH_ENRICHMENT_PAGE_SIZE`
- `SOORIN_GRAPH_ENRICHMENT_REFRESH_SECONDS`
- `SOORIN_GRAPH_ENRICHMENT_RETRY_SECONDS`
- `SOORIN_GRAPH_ENRICHMENT_POLL_INTERVAL_SECONDS`
- `SOORIN_GRAPH_ENRICHMENT_STARTUP_DELAY_SECONDS`
- `SOORIN_GRAPH_ENRICHMENT_MAX_PAGES_PER_CYCLE`
- `SOORIN_GRAPH_ENRICHMENT_LEASE_TTL_SECONDS`
- `SOORIN_GRAPH_ENRICHMENT_SHUTDOWN_TIMEOUT_SECONDS`

These settings bound the periodic Product detection-overview enrichment pipeline.

## Graph retrieval and refresh policy

The graph configuration includes:

- raw snapshot path;
- structured asset-search default/max limits;
- model-facing structured search/aggregate token budgets;
- model-facing structured search row bound;
- UI/node/edge limits;
- node-summary and neighbor scopes;
- one-hop/two-hop/full-neighbor bounds;
- path length limit;
- graph-context token limit;
- comparison enumeration limits;
- automatic refresh interval/startup/jitter/failure/snapshot/lock settings.

The exact current variable names and defaults are maintained in `.env.example`.

Important structured context variables are:

- `SOORIN_CONTEXT_STRUCTURED_ASSET_SEARCH_MAX_TOKENS`
- `SOORIN_CONTEXT_STRUCTURED_ASSET_AGGREGATE_MAX_TOKENS`
- `SOORIN_CONTEXT_STRUCTURED_ASSET_SEARCH_MAX_ROWS`

They control model-facing serialization only; they do not change Neo4j's matched count.

## RAG and embeddings

- `SOORIN_RAG_ENABLED`
- `SOORIN_RAG_SOURCE_ROOT`
- `SOORIN_RAG_BACKEND`
- `SOORIN_RAG_COLLECTION`
- `SOORIN_RAG_TOP_K`
- `SOORIN_RAG_SCORE_THRESHOLD`
- `SOORIN_RAG_QDRANT_MODE`
- `SOORIN_RAG_QDRANT_URL`
- `SOORIN_RAG_QDRANT_PATH`
- `SOORIN_RAG_QDRANT_API_KEY`
- `SOORIN_RAG_QDRANT_TIMEOUT_SECONDS`
- `SOORIN_RAG_EMBEDDING_MODEL`
- `SOORIN_RAG_EMBEDDING_DIMENSION`
- `SOORIN_RAG_EMBEDDING_LOCAL_FILES_ONLY`
- `SOORIN_RAG_EMBEDDING_CACHE_DIR`
- `SOORIN_RAG_EMBEDDING_REVISION`
- `SOORIN_RAG_DISTANCE`
- `SOORIN_RAG_MAX_CONTEXT_TOKENS`
- `SOORIN_RAG_CHUNK_SIZE_CHARS`
- `SOORIN_RAG_CHUNK_OVERLAP_CHARS`
- `SOORIN_RAG_UPSERT_BATCH_SIZE`

The Compose API overrides Qdrant/cache paths to container paths. `make preflight` validates local Qdrant metadata and the configured embedding snapshot/dimension before deployment.

## Conversation and short-term context

Configuration includes:

- chat-history persistence;
- maximum transcript messages;
- maximum current conversation messages;
- recent raw-message target;
- summary enable/trigger/token/temperature/timeout settings;
- relevant-turn limit and token budget;
- episode retention/context limits;
- overall memory-context budget.

Use `.env.example` for the exact current variable names and defaults.

## Long-term memory

- `SOORIN_LONG_TERM_MEMORY_ENABLED`
- `SOORIN_LONG_TERM_MEMORY_BACKEND`
- `SOORIN_MEMORY_VECTOR_INDEX_ENABLED`
- `SOORIN_MEMORY_QDRANT_COLLECTION`
- `SOORIN_MEMORY_RETRIEVAL_CANDIDATE_K`
- `SOORIN_MEMORY_RETRIEVAL_TOP_K`
- `SOORIN_MEMORY_MIN_SCORE`
- `SOORIN_MEMORY_RERANK_ENABLED`
- `SOORIN_MEMORY_RERANK_MODEL`
- `SOORIN_MEMORY_RERANK_TIMEOUT_SECONDS`
- `SOORIN_MEMORY_CONTEXT_LONG_TERM_TOKEN_BUDGET`
- `SOORIN_MEMORY_AUTO_PROMOTION_ENABLED`
- `SOORIN_MEMORY_PROMOTION_POLICY_VERSION`
- `SOORIN_MEMORY_ACTIVE_VALIDITY_SECONDS`

The Product backend is the canonical production LTM authority when `SOORIN_LONG_TERM_MEMORY_BACKEND=product`. SQLite remains a local/reference option.

## Thread state and local persistence

- `SOORIN_THREAD_STATE_BACKEND`
- `SOORIN_LOCAL_PRODUCT_SIMULATION_ENABLED`
- `SOORIN_STREAMLIT_AUTH_BACKEND`
- `SOORIN_LOCAL_TEST_USER_CREATION_ENABLED`
- `SOORIN_LOCAL_PRODUCT_TEST_USER_ID`
- `SOORIN_LOCAL_SQLITE_PATH`
- local conversation/message and memory retention limits
- `SOORIN_LANGGRAPH_CHECKPOINT_BACKEND`

For integrated production continuity, configure the Product Thread-State backend according to the Product memory contract.

## Human trace and evidence diagnostics

- `SOORIN_HUMAN_TRACE_ENABLED`
- `SOORIN_HUMAN_TRACE_DETAIL`
- `SOORIN_EVIDENCE_SNAPSHOT_ENABLED`
- `SOORIN_EVIDENCE_SNAPSHOT_MODE`
- `SOORIN_EVIDENCE_SNAPSHOT_ROOT`
- `SOORIN_EVIDENCE_SNAPSHOT_TTL_HOURS`
- `SOORIN_EVIDENCE_SNAPSHOT_MAX_REQUESTS`
- `SOORIN_EVIDENCE_SNAPSHOT_MAX_TOTAL_BYTES`
- `SOORIN_EVIDENCE_SNAPSHOT_MAX_BYTES`

Container deployment places evidence snapshots under `/workspace/data/runtime/evidence`.

## Hugging Face runtime controls

- `HF_HUB_DISABLE_TELEMETRY`
- `HF_HUB_OFFLINE`
- `TRANSFORMERS_OFFLINE`

Compose can force offline behavior for the API while native development remains configurable.

## Server synchronization rule

Before deploying a new revision:

1. compare the server `.env` key set with that revision's `.env.example`;
2. bring over new behavioral variables and validated values from the tested environment;
3. preserve server-only paths, addresses, and secrets;
4. verify the image tag and Hugging Face host-cache path;
5. run `make show-config`, `make config`, and `make preflight` before build/start.
