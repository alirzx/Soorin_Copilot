# Soorin Copilot Current Architecture

Audit date: 2026-08-05.

This document describes the implemented repository state. It distinguishes working behavior from partial foundations, placeholders, and deferred work. It should be updated after architecture-changing code changes.

## 1. Architecture Principles

Soorin Copilot follows these implementation rules:

- Deterministic systems own entity identity, provider selection validation, graph retrieval, product evidence retrieval, context budgeting, state updates, and fallback safety.
- The LLM is the primary semantic router and final synthesis engine, but it does not invent operational evidence.
- Current operational evidence outranks previous assistant prose and retrieved documentation.
- Explicit user entities outrank UI-selected entities, which outrank active session entities.
- Detached general questions must not inherit stale asset context.
- Provider failures are represented as unavailable, partial, stale, or safe failure; they are not converted into verified facts.
- Graph evidence is topology evidence only. It does not prove physical routing, trust, dependency, compromise, or reachability.
- RAG evidence is documentation evidence only. It does not override current product or graph evidence.

## 2. High-Level Runtime Flow

```mermaid
sequenceDiagram
    participant UI as Streamlit UI
    participant API as FastAPI
    participant Service as CopilotService facade
    participant Workflow as Bounded LangGraph
    participant Entity as EntityResolver
    participant Router as SemanticIntentRouter
    participant Planner as Bounded Planner
    participant Executor as Capability Executor
    participant Reviewer as Evidence Reviewer
    participant Providers as Evidence providers
    participant Composer as ContextComposer
    participant LLM as Arvan chat deployment

    UI->>API: POST /chat or /chat/stream
    API->>Service: message, resolved request identity, optional ui_context
    Service->>Workflow: invoke request with runtime dependencies
    Workflow->>Entity: resolve explicit/UI/session entities
    Workflow->>Router: classify with configured Kimi/GLM/GPT deployment
    Router->>Workflow: JSON route decision
    Workflow->>Workflow: validate and normalize task
    Workflow->>Planner: multi-step only; one structured proposal
    Workflow->>Workflow: deterministic plan validation/fallback
    Workflow->>Executor: validated registered read-only plan
    Executor->>Providers: dependency-aware bounded execution
    Providers->>Executor: canonical ToolResults with raw provider objects preserved
    Executor->>Reviewer: canonical EvidencePack
    Reviewer->>Executor: at most one approved supplemental retrieval
    Reviewer->>Workflow: sufficient, limitations, missing evidence, or safe failure
    Workflow->>Composer: bounded model context
    Composer->>Workflow: tagged dynamic context
    Workflow->>LLM: final chat request or stream
    LLM->>API: answer deltas or answer text
    API->>UI: envelope or UTF-8 SSE
```

## 3. API Layer

Implemented in `app/src/api`.

Routes:

- `GET /health`: returns `{"status": "ok"}`.
- `GET /llm/health`: returns configuration/provider readiness only; no expensive generation call.
- `POST /chat`: non-streaming chat envelope.
- `POST /chat/stream`: final-answer SSE stream.
- `GET /graph/status`: active graph status and refresh metadata.
- `GET /graph/stats`: graph statistics.
- `GET /graph/nodes/{ip}`: single-node degree lookup.
- `GET /graph/nodes/{ip}/neighbors`: bounded direct neighbors.
- `GET /graph/nodes/{ip}/context`: deterministic context digest.
- `GET /graph/path`: shortest path in the observed directed communication graph.

`app/src/api/main.py` creates one FastAPI app, includes chat and graph routers, validates selected LLM deployments on startup, loads the last-known-good graph artifact, and starts the graph refresh scheduler.

Current API compatibility:

- `/chat` keeps the existing envelope shape.
- `/chat/stream` preserves the existing SSE event schema and adds UTF-8 charset.
- There are graph read endpoints, but no RAG endpoint and no planner endpoint.
- `GET /health` is public. Other current API routes require
  `SOORIN_COPILOT_API_KEY`, accepted either as a standard Bearer credential or
  as `Soorin_copilot_api_key` when a Product JWT must remain in `Authorization`.

## 4. Configuration

Implemented in `app/src/config/settings.py`.

Settings are loaded from `app/.env` if present, then from environment variables. The local `.env` file is intentionally not tracked. Sensitive values are represented in logs only as configured/not configured booleans.

Major groups:

- API/UI: `API_HOST`, `API_PORT`, `API_RELOAD`, `SOORIN_API_BASE_URL`, `SOORIN_API_TIMEOUT_SECONDS`, `STREAMLIT_SERVER_PORT`.
- LLM: selected router/chat deployments plus per-deployment Arvan base URL, model, API key, timeouts, token limits, sampling support, and conservative estimate multiplier.
- Product API: base URL, topology path, profile path, detection path, login path, token, login credentials, captcha bypass, HWID, retry and timeout settings.
- Graph: artifact paths, UI limits, API limits, retrieval limits, context limits, refresh policy, validation thresholds.
- RAG: enabled flag, source root, backend, Qdrant mode/server URL/local path, collection, score threshold, BGE model/dimension/revision/cache/local-only policy, chunking and upsert limits.
- Prompts and router: system prompt path, router prompt path, confidence threshold, repair retry flag.
- Conversation: history storage and deterministic summary limits.
- Local persistence: disabled-by-default Product chat simulation, memory/SQLite
  thread-state backend, local SQLite path, and reserved LangGraph checkpoint
  backend selection.
- Observability: console/JSON terminal logs, bounded rotating UTF-8 file logs, summary/detailed human traces, TTY-aware color, and disabled-by-default evidence snapshots.

Validation currently enforces:

- Selected enabled LLM deployments must have base URLs.
- Product profile path must contain exactly one safe `{ip}` placeholder.
- RAG Qdrant local mode requires a path.
- RAG Qdrant server mode requires a URL.
- BGE default dimension must be 768.
- Legacy SecureBERT model and collection values are rejected when RAG is enabled.
- Log format, color, human-trace detail, and evidence snapshot modes are validated.
- Local chat or SQLite thread-state mode requires a non-empty local database path.

## 5. LLM Deployments

Implemented in `app/src/config/llm_deployments.py`, `app/src/core/llm/client.py`, and `app/src/core/llm/providers/arvan.py`.

The system supports three named OpenAI-compatible deployments through the existing adapter:

- `kimi`, default model label `kimi-k3`.
- `glm`, default model label `GLM-5.2`.
- `gpt55`, default model label `GPT-5.5`.

The LLM configuration is role-based: `SOORIN_ROUTER_*`, `SOORIN_PLANNER_*`, and `SOORIN_SYNTHESIZER_*` independently configure the semantic Router, optional Planner, and final Synthesizer. All roles use the shared OpenAI-compatible transport settings and the typed `LLMRoleConfig` contract. The current example intentionally uses `CHANGE_ME_MODEL` placeholders; real deployment URLs, models, and keys are supplied only through private runtime configuration.

The provider:

- Calls `{base_url}{chat_path}`.
- Sends OpenAI-compatible `messages`.
- Uses `Authorization: <auth_scheme> <api_key>`.
- Extracts `choices[0].message.content`.
- Detects `reasoning_content` without logging or exposing it unless explicit reasoning exposure is enabled.
- Logs compact metadata: purpose, deployment, model, host, status, latency, usage, output size, and reasoning presence.
- Does not log API keys, Authorization headers, raw responses, or hidden reasoning.

Retry behavior:

- Router calls are not retried through the generic transient retry loop.
- Final chat calls can retry transient provider failures up to configured limits.
- Streaming is available only for final chat purpose.

## 6. Entity Resolution and Authority

Implemented in `app/src/core/context/entities.py`.

Entity extraction is deterministic and currently focused on IPv4 addresses and CIDR constraints.

Authority order:

```text
explicit IP in current message > selected UI IP > active session IP/entities
```

Supported behavior:

- Explicit single IP creates a message-sourced entity.
- Explicit pair creates a message-sourced pair for relationship, comparison, and path routes.
- UI-selected IP is used only when no valid explicit message entity exists.
- Active session IP supports follow-ups such as "its data", "its connections", "tell me more about it".
- Active pair supports pair follow-ups such as "compare them".
- Topic detachment suppresses stale active/UI context for general conceptual questions.
- Explicit new IP overrides previous active IP and UI selection.

The router classifies intent only. It does not invent entities; it can choose which already-resolved binding should be used.

## 7. Semantic Routing

Implemented in `app/src/core/context/intent.py` and `app/src/core/context/router.py`.

Normal routing path:

```text
deterministic entity extraction
-> semantic LLM router
-> strict schema validation
-> deterministic normalization
-> deterministic fallback only on provider/schema/confidence failure
```

The external router prompt is in `app/prompts/intent_router_system_prompt.md`. A compact fallback router prompt exists in code if the file is missing.

Validated router fields include:

- intent
- scope
- direction
- depth
- requires_graph
- requires_detection
- requires_asset_profile
- requires_knowledge
- entity_binding
- requires_multiple_entities
- is_followup
- classification_confidence
- reason

Important normalization rules:

- Single-asset `node_summary` always requires graph context.
- Graph scopes require graph context.
- General or unclear turns bind no stale entity.
- Explicit entities override UI binding.
- UI binding applies only when no explicit entity exists.
- Exhaustive direct-neighbor wording maps to `full_neighbors`.
- Path requires two entities and graph context.

Deterministic fallback is present but intentionally secondary. Its operational provider logging currently covers graph, detection, and asset profile; semantic routes can also select knowledge.

## 8. Durable Bounded Investigation Workflow

Implemented in `app/src/core/agent`.

Current status: Phase 3 typed LangGraph execution is the normal request path. `CopilotService` is a facade and no longer contains the former `_chat_phase2` workflow controller.

Contracts:

- `InvestigationState`
- `TaskSpec`
- `ExecutionPlan`
- `PlanStep`
- `ToolResult`
- `EvidenceFact`
- `EvidencePack`
- `ReviewDecision`
- `CapabilitySpec`

`TaskSpec.recommended_steps` is a semantic complexity recommendation. Its temporary read-only `max_steps` property is compatibility-only. Hard limits remain in `ExecutionPlan.maximum_allowed_calls`, configured agent limits, and `PlanValidator`.

Capability registry entries:

- `asset.get_profile`
- `asset.get_detection`
- `graph.get_summary`
- `graph.get_neighbors`
- `graph.get_relationship`
- `graph.compare_assets`
- `graph.find_path`
- `knowledge.search`

Active graph nodes:

```text
resolve_entities
-> route
-> validate_task
-> build_direct_plan OR build_plan
-> validate_plan
-> build_fallback_plan when required
-> dispatch_specialists
   -> Asset Investigation Specialist for validated asset.* steps
   -> Graph Analysis Specialist for validated graph.* steps
   -> generic execution for validated non-specialist steps
-> join_specialist_results in parent-plan order
-> build_evidence
-> review_retrieval
-> supplemental_retrieval at most once
-> compose_context
-> review_context
-> synthesize
-> update_memory
```

- Direct tasks use `compile_direct_plan`; the Planner is skipped.
- Multi-step tasks use the existing provider-neutral LLM client with the role-based `SOORIN_PLANNER_*` settings.
- Planner output is a proposal. It cannot execute tools, invent entities, introduce URLs, change permissions, or mutate state.
- Planner receives one model call only. A rejected plan may receive one deterministic mechanical repair for known structural defects; malformed model output falls back safely without another Planner call.
- Planner failure or plan rejection falls back explicitly to a deterministic plan when one is safe.
- Router failure with exactly two deterministically resolved entities preserves explicit comparison, direct-relationship, or path meaning before prior-route continuity. Normal comparison fallback includes per-entity Profile and Detection plus `graph.compare_assets`; direct relationships and paths remain graph-specific.
- `PlanValidator` enforces known/planner-visible/read-only capabilities, entity authority, cardinality, argument schemas, dependency references, DAG structure, duplicate-call rejection, six-call maximum, two-entity maximum, and graph depth two.
- `CapabilityExecutor` runs independent steps concurrently with a maximum of four workers by default. Profile and Detection are serialized because they share a Product client/session; Graph and Knowledge can overlap safely.
- Asset and Graph Specialists are typed bounded LangGraph subgraphs. They receive only validated parent-plan steps, delegate execution to the same registry/executor, add no LLM calls, cannot mutate task/entity authority or memory, and return compact serializable summaries. Parent review and synthesis remain authoritative.
- Provider exceptions become safe typed failures; successful and partial results are preserved.
- LangGraph is bounded at recursion limit 32. Conditional edges express direct/planner selection, clarification/safe failure, deterministic plan fallback, one supplemental retrieval, context review, and terminal memory update.
- Runtime clients, locks, streams, and secrets are supplied through LangGraph runtime context and are never persisted in `InvestigationState`.
- The deterministic compatibility runner uses the same node contract when LangGraph is unavailable. The historical callback adapter exists only for focused backward-compatible tests.
- There is no unrestricted tool loop or autonomous action path.

Terminal status is evidence-driven. `completed` means every required validated capability and final synthesis succeeded without material fallback or truncation. `completed_with_limitations` is reserved for material provider/evidence gaps, truncation, deterministic synthesis fallback, or recovered plan fallback and carries bounded `limitation_reasons`. Clarification, partial failure, failure, and cancellation remain distinct terminal outcomes.

### Workflow persistence model

LangGraph still compiles without a checkpointer. The installed official synchronous
SQLite saver is compatible with the current synchronous invocation API, but the
current `InvestigationState` contains model messages and rich evidence objects that
Gate 3 does not permit to be serialized. `SOORIN_LANGGRAPH_CHECKPOINT_BACKEND=sqlite`
therefore logs an explicit deferral and does not open a checkpoint database.
Interrupted workflows cannot resume after a restart.

Separately, Gate 5 provides an opt-in local SQLite `ChatRepository` and bounded
`ThreadStateStore`. These adapters do not persist arbitrary LangGraph state. They
store owner-scoped transcripts/request status plus one versioned
`ThreadMemoryState` with routing continuity, working-summary provenance, bounded
turn references, and bounded episode summaries. They are disabled by default.

### Service responsibilities after refactor

```text
CopilotService
-> request/session/trace preparation
-> request-scoped runtime dependency construction
-> BoundedCopilotWorkflow invocation
-> SSE stream forwarding and response adaptation
-> top-level safe error translation and usage-report lifecycle

BoundedCopilotWorkflow
-> explicit nodes and conditional edges listed above
-> bounded request lifecycle and safe terminal boundaries
-> workflow and node observability
```

The service contains no legacy direct provider loop. Normal Profile, Detection, Graph, and Knowledge calls occur only through registered capability handlers. The service consumes provider results only after they have been converted to `ToolResult` and associated with the EvidencePack.

### Planner behavior

Direct requests always use deterministic plans and do not call the Planner. Multi-step requests call the Planner only when `SOORIN_PLANNER_ENABLED=true`. The tracked prompt is `app/prompts/planner_system_prompt.md`; its capability catalog is generated from the registry. Planner output is JSON-only, receives no generic transient retry, and is never sent to a second Planner call. Extra prose or malformed JSON is rejected. Valid JSON with a mechanically repairable structural defect may be repaired once in deterministic code, revalidated, then replaced by a logged deterministic fallback plan if still invalid.

The Planner cannot execute tools. Entity authority, known capability selection, read-only policy, entity cardinality, Pydantic arguments, dependency references, DAG validity, duplicate calls, graph depth, and call budgets remain deterministic `PlanValidator` responsibilities.

### Capability runtime and execution

The registry, validator, and executor are initialized once. Tests may replace provider objects; in that case `CopilotService` atomically rebuilds all three under a lock and each request retains one consistent runtime snapshot. Normal requests do not rebuild the registry.

The executor:

- accepts only plans marked validated by the application path;
- schedules dependency-ready steps in deterministic plan order;
- overlaps independent steps up to the configured worker bound;
- serializes Product Profile and Detection through the `product` concurrency lock;
- allows Graph and Knowledge to overlap;
- skips every downstream step whose dependency failed, including optional steps;
- preserves partial and successful results when another step fails;
- applies capability and total request time bounds;
- always shuts down its thread pool.

Already-running synchronous provider threads cannot be forcibly terminated after timeout. Native Product/Qdrant transport timeouts remain the primary hard bounds, and a timed-out request does not treat late work as evidence.

### ToolResult and EvidencePack

`ToolResult` is the canonical capability-output contract. It includes a stable model-context identity plus explicit inclusion, omission reason, representation, and token metadata. Raw provider result objects remain internal in `raw_payload`/`provider_result`; the model receives only the validated Product projection selected for unresolved evidence classes. Full Product JSON is model-facing only for an explicit validated `full` view. Graph retrieval/serialization completeness, counts, truncation, and limitations survive conversion. Knowledge chunks, scores through the original result, citations, counts, backend, and freshness survive conversion. Unknown provider statuses fail closed as `invalid`; they are never normalized to success.

### Memory-first evidence-gap policy (Gate 8)

After `TaskSpec`, deterministic code derives typed evidence requirements. The
`MemorySufficiencyGate` evaluates coverage, exact entity binding, authority,
freshness class, completeness, and active contradiction. Its explicit outcomes
are `memory_sufficient`, `memory_sufficient_verification_required`,
`live_evidence_required`, `contradictory_memory`, and `memory_unavailable`.
Semantic similarity never establishes entity identity or authority. Current
Detection, Graph, risk, and activity requirements normally retain a live
verification call. Only fully satisfied revision-based or explicitly historical
requirements may skip a call. A skip is represented by a validated memory-backed
`ToolResult`, logged, shown in Human Trace, and accepted by PlanValidator; it is
never treated as an absent capability.

Both direct and Planner plans converge on the same deterministic gap application,
`ViewSelector`, and `PlanValidator`. Neither Router nor Planner can supply raw
URLs, disable freshness checks, or force unvalidated full payloads.

`EvidencePack` is constructed only from `ToolResult` records. It carries request/trace/plan IDs, resolved entities, plan summary, capability coverage, identity-keyed result coverage, graph completeness, citations, missing evidence, limitations, contradictions, supplemental history, and review outcome. Repeated capabilities remain separate ToolResults.

### Supplemental validation

The only allowed sequence is implemented:

```text
Reviewer proposes a registered capability and final arguments
-> compile the complete one-step supplemental plan
-> PlanValidator validates the final step
-> CapabilityExecutor executes the validated plan
```

Arguments are never replaced after validation. Duplicate equivalent calls are rejected before execution, the supplemental plan has a one-call budget, and `allow_supplemental=false` after the first attempt makes a second retrieval impossible.

### Planner and execution settings

- `SOORIN_PLANNER_ENABLED`
- `SOORIN_ROUTER_*`, `SOORIN_PLANNER_*`, and `SOORIN_SYNTHESIZER_*`
- `SOORIN_PLANNER_REPAIR_ENABLED`
- `SOORIN_AGENT_MAX_SUPPLEMENTAL_RETRIEVALS` (hard-capped at 1)
- `SOORIN_AGENT_MAX_CAPABILITY_CALLS` (hard-capped at 6)
- `SOORIN_AGENT_MAX_ENTITIES` (hard-capped at 2)
- `SOORIN_AGENT_MAX_GRAPH_DEPTH` (hard-capped at 2)
- `SOORIN_AGENT_EXECUTOR_MAX_CONCURRENCY` (hard-capped at 4)
- `SOORIN_AGENT_REQUEST_TIMEOUT_SECONDS`

`SOORIN_PLANNER_ENABLED` defaults to `false`. One Planner proposal is an architectural fixed bound rather than a misleading configurable planning-pass value. Enable Planner testing explicitly with `SOORIN_PLANNER_ENABLED=true` and provide the private `SOORIN_PLANNER_*` role configuration.

## 9. Evidence Reviewer

Implemented in `app/src/core/agent/reviewer.py`.

Current status: deterministic reviewer is authoritative before synthesis.

Checks include:

- Required capability present.
- Required entity cardinality present.
- Provider status.
- Stale or unknown freshness.
- Completeness.
- Truncation.
- Context inclusion.
- Contradictions.
- Limitations.
- RAG configured/available state through `ToolResult` status and limitations.

Outcomes:

- `sufficient`
- `answer_with_limitations`
- `missing_required_evidence`
- `safe_failure`

There is no LLM evidence reviewer.

Graph completeness is evaluated in authority order: `complete_for_user_request`, then `requested_scope_complete`, then `serialized_context_complete_for_retrieved_subset`, and only then general retrieval completeness. A complete `node_summary` remains complete when a broader neighborhood was bounded; that broader truncation is retained as an informational limitation.

Graph scope, traversal, retrieval, and model serialization are separate contracts. `node_summary` serializes target identity and aggregate degree/direction/subnet/importance metadata without a hidden peer sample. One-hop and full-neighbor retrieval use their configured safety ceilings; two-hop uses its own node setting and a named code-level edge ceiling. Relationship and path are pair-specific. Comparison retains both summaries, shared/distinct peer totals, degree/subnet differences, and a protected direct relationship fact block. Optional comparison peer and neighborhood edge details may be truncated or omitted by budget, but direct relationship truth is serialized independently before optional details. Every result uses a capability/entity/scope identity; commutative comparison identities normalize entity order, while path/relationship order is preserved.

If all required evidence is unavailable, synthesis is skipped and the application returns a deterministic safe-failure explanation. If some valid evidence remains, the Reviewer allows synthesis with explicit limitations. A missing, not-yet-executed required capability may trigger one supplemental call; duplicate equivalent retrieval is rejected and a second supplemental call is impossible.

Two review stages have distinct responsibilities:

1. Retrieval review checks required capability execution, entity coverage, status, freshness, completeness, truncation, limitations, contradictions, and whether one bounded supplemental retrieval can fill a missing capability.
2. Context review runs after Context Composer budgeting updates each `ToolResult.context_included` value. It decides whether synthesis can use the evidence actually present in model context.

The retrieval stage may repeat once after an approved supplemental call. That repeat is not a second autonomous review loop. The final context review never authorizes another retrieval.

## 10. Product API Integration

Implemented in `app/src/core/product_client` and product-backed context providers.

Product client behavior:

- Uses `requests.Session` with retry adapter.
- Sends bearer token and HWID headers.
- Normalizes configured bearer tokens so operators may provide either raw token or `Bearer <token>`.
- Can bootstrap from `SOORIN_PRODUCT_API_TOKEN`.
- Can login through `SOORIN_PRODUCT_LOGIN_PATH` when credentials are configured.
- On 401, invalidates token, retries once after refresh/login, then returns a safe unauthorized error.
- Logs auth source, token age, refresh status, retry count, status code, and latency without logging tokens, passwords, captcha bypass values, or Authorization headers.

Product endpoints:

- Topology: `SOORIN_PRODUCT_TOPOLOGY_PATH`, default `/zeek/connections/unique-ip-pairs`.
- Detection full: `SOORIN_PRODUCT_ASSET_DETECTION_PATH`, default `/asset-detection/test/{ip}`.
- Detection overview/evidence/similarity/cluster: the four
  `SOORIN_PRODUCT_ASSET_DETECTION_*_PATH` settings, defaulting to
  `/asset-detection/{ip}/{view}`.
- Asset profile: `SOORIN_PRODUCT_ASSET_PROFILE_PATH`, default `/profile/{ip}`.
- Login: `SOORIN_PRODUCT_LOGIN_PATH`, default `/auth/login`.

Detection and profile providers:

- Profile fetches the current full `/profile/{ip}` JSON once per entity/request
  and preserves it unchanged internally. The Product profile-overview endpoint
  is intentionally not used.
- Detection fetches only selected `overview`, `evidence`, `similarity`, `cluster`,
  or deep `full` views. Every view uses the same Product session, login/token
  refresh, retry, Bearer authorization, and `x-hwid` machinery.
- Cache keys include normalized IP and Detection view.
- Can return stale cached evidence on provider error if configured.
- Track raw JSON size, approximate tokens, top-level key counts, cache hit/miss/stale status, HTTP status, and safe error classification.
- Create safe path/type/length inventories and deterministic model projections.

Detection supports `overview`, `evidence`, `similarity`, `cluster`, and `full`.
Similarity means rule/tag/role affinity, not embedding-space similarity. Cluster
means rule/tag/role-affinity grouping, not unsupervised ML clustering; population
one is weak cohort evidence. Profile supports deterministic `overview`,
`identity`, `security`, `network`, `activity`, and `full` projections from the
current full endpoint. Large lists carry total/included/omitted counts and a
stable selection rule. `full` is deep/exhaustive-only by default.

## 11. Graph Topology

Implemented in `app/src/core/graph`.

Graph source:

- Product topology unique IP pairs.
- Parsed into `TopologyConnectionRecord`.
- Built as a directed `networkx.DiGraph`.
- Edge `src_ip -> dst_ip` means an observed unique communication pair.
- Duplicate records increase edge weight.

Artifacts:

- Raw JSON: `SOORIN_GRAPH_RAW_PATH`.
- Pickle graph: `SOORIN_GRAPH_PICKLE_PATH`.
- Stats JSON: `SOORIN_GRAPH_STATS_PATH`.
- Optional GraphML: `SOORIN_GRAPH_GRAPHML_PATH`.
- Optional GEXF: `SOORIN_GRAPH_GEXF_PATH`.

Startup and refresh:

- API startup loads the last-known-good pickle if available.
- Background refresh is controlled by `SOORIN_GRAPH_AUTO_REFRESH_ENABLED`, `SOORIN_GRAPH_REFRESH_ON_STARTUP`, interval, jitter, failure, and validation settings.
- Refresh fetches topology, validates minimum nodes/edges and drop ratios, writes artifacts atomically, writes snapshots, prunes old snapshots, and atomically replaces the active in-memory graph.
- Failed refresh preserves the previous active graph.

Retrieval scopes:

- `node_summary`: summary of one node; context metadata reports only included summary context, not peer entries.
- `one_hop`: direct peers.
- `full_neighbors`: exhaustive direct-neighbor retrieval within configured limits.
- `two_hop`: neighbors of neighbors within configured limits.
- `multi_entity_comparison`: pair comparison and shared peers.
- `path`: shortest observed communication-graph path.

Graph limitations are always attached to Copilot graph context.

## 12. Graph API and Visualization

Graph API endpoints are read-only and deterministic. They use the active in-memory graph or the configured graph artifact.

The Streamlit topology page:

- Renders an interactive PyVis/vis-network graph.
- Supports max-node, minimum-degree, subnet, and physics-mode filters.
- Uses canonical CIDR subnet labels and filters with `ipaddress.ip_network(..., strict=False)`.
- Skips non-IP nodes safely.
- Keeps tab output scoped to the proper Streamlit tab.
- Supports node-click selection for Copilot UI context.
- Clears node selection when the graph canvas background is clicked.
- Preserves zooming, dragging, node details, shortest path lookup, IP exploration, and all nodes views.

The visualization uses explicit vis-network physics options, stable random seed, and a default "stabilize once, then freeze" mode.

## 13. RAG and Knowledge Search

Implemented in `app/src/core/rag`.

Current status: optional foundation implemented; indexing is explicit; no startup indexing.

Responsibilities:

- `sources.py`: source root validation, source discovery, document loading.
- `chunker.py`: deterministic chunking with stable chunk metadata.
- `embeddings.py`: lazy Hugging Face embedder behind `Embedder` protocol.
- `vector_store.py`: Soorin-owned vector-store protocol.
- `qdrant_store.py`: Qdrant implementation behind the vector-store protocol.
- `service.py`: `knowledge.search` orchestration.
- `citations.py`: citation creation.
- `safety.py`: prompt-injection filtering on retrieved chunks.
- `indexer.py`: explicit validation/indexing entry point.

Default model:

- `BAAI/bge-base-en-v1.5`
- dimension `768`
- attention-mask-aware mean pooling
- L2 normalization
- identical document and query embedding pipelines
- lazy loading on first real embedding call
- pinned revision and optional cache directory forwarded to both tokenizer and model
- strict `local_files_only` loading by default with no network fallback

Qdrant:

- Primary vector backend.
- Supports `server` mode with URL/API key and `local` mode with a local path.
- Validates collection dimension and distance.
- Supports stable IDs, payload metadata, simple payload filters, batched upsert, delete, health, and collection info.
- Does not launch a Qdrant server by itself.

When local-only embedding is enabled, a missing model or revision cache entry returns a safe classified unavailable result. Compose mounts the host Hugging Face cache read-only and sets `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`, and telemetry disable flags.

Knowledge statuses:

- `ok`
- `empty`
- `not_configured`
- `unavailable`
- `invalid`
- `partial`

RAG is appropriate for SOC runbooks, NDR documentation, MITRE/protocol explanations, hardening guidance, investigation procedures, and approved product documentation. It is not source of truth for current asset identity, current graph relationships, live detections, current alerts, risk values, or exact current peer lists.

Knowledge normally permits one search. A second call requires a distinct approved purpose and meaningfully different normalized query. More than two or equivalent calls are rejected before execution; accepted chunks and citations are aggregated and deduplicated.

## 14. Context Composition and Budgeting

Implemented in `app/src/core/context/composer.py`.

The context composer produces a dynamic system message with:

- Provider coverage manifest.
- Tagged asset profile JSON.
- Tagged asset detection JSON.
- Tagged graph JSON.
- Tagged knowledge JSON and citations when selected and budget permits.
- Source semantics and limitations.
- Explicit warning that operational evidence outranks documentation.
- A compact reviewed-EvidencePack summary containing plan identity, provider coverage, graph completeness, review outcome, missing evidence, contradictions, and limitations.

The composer input is rebuilt from canonical reviewed `ToolResult` objects.
Complete provider objects remain unchanged internally; `view_payload` is the
deterministic model projection. Exact same-key/value identity facts are collapsed
with source/path support, while contradictions, different timestamps, and
current-versus-historical observations remain distinct. Compatible delta context
requires an entity/view/schema-matched complete baseline already accessible in
current memory; otherwise the compact current view is sent.

Final synthesis receives the global system prompt, a compact reviewed EvidencePack summary, dynamic context reconstructed from EvidencePack provider results, bounded conversation history, and the current user request. No old provider loop or raw provider side channel can add current evidence outside that boundary. If retrieval review, context review, or required graph-context budgeting produces a safe-failure condition, the final LLM is not called. Streaming and non-streaming requests share this same orchestration and differ only in final model transport.

Budget controls:

- Uses configured context window, reserved output tokens, safety margin, and base input token estimate.
- Evidence-class caps bound Profile and Detection, and Graph capacity is reserved
  before Product sections. Operational evidence outranks optional Knowledge.
- Full Product payloads cannot silently starve required Graph evidence.
- Required graph context that cannot fit creates a safe context limitation.
- Knowledge context participates in the same budget and may be omitted with logged reason.

Gate 8 offline fixture measurements use the repository's conservative
`characters / 4` estimator; they are regression indicators, not measured
production tokenizer savings:

| Request shape | Selected views | Full estimate | Compact estimate | Estimated reduction |
| --- | --- | ---: | ---: | ---: |
| Quick asset summary | Profile overview | 11,306 | 56 | 99.5% |
| Classification | Detection overview | 11,561 | 59 | 99.5% |
| Classification explanation | Detection overview + evidence | 11,561 | 165 | 98.6% |
| Identity / AD analysis | Profile identity | 11,306 | 104 | 99.1% |
| Risk analysis | Profile security | 11,306 | 126 | 98.9% |
| Similarity | Detection similarity | 11,561 | 173 | 98.5% |
| Cluster | Detection cluster | 11,561 | 168 | 98.5% |
| Network activity | Profile network + activity | 11,306 | 169 | 98.5% |
| Deep investigation | Detection full | 11,561 | 11,561 | 0.0% |

Input estimates are deployment/model labeled and multiplied by `SOORIN_LLM_TOKEN_ESTIMATE_MULTIPLIER` (default `1.35`). Output reservation is dynamic: brief uses 1,536 tokens, standard uses 4,096, and deep/report uses up to 6,144, always capped by the deployment. Immediately before synthesis, a hard guard enforces `calibrated input + selected output reservation + configured safety margin <= context window`. It removes eligible history, performs one bounded context recomposition, and may reduce output only to a detail-policy floor. If the invariant still fails, the provider is not called. Traces report remaining-before-safety and remaining-usable tokens separately.

Conversation history is trimmed to fit budget, preferring current evidence over old assistant claims.

## 15. Conversation and Routing State

Implemented in `app/src/core/memory`.

The default remains in-memory per process. When explicitly enabled for local
development, SQLite persists Product-chat simulation records and bounded thread
memory across process restarts. Full workflow execution state remains process-local.

Conversation memory:

- Stores user and assistant messages when enabled.
- Truncates to configured maximum messages.
- Uses a typed `MemoryContextKey` over normalized entities, topic family, relationship mode, and scope family.
- Keeps bounded current Working Memory and in-process Episodic Session Memory.
- Builds deterministic compact summaries when token thresholds are exceeded or an episode closes.
- Detaches raw history when entity, pair, or topic changes; a previous episode summary re-enters only for a matching context key.
- Retains bounded old episode records without treating them as current provider evidence.
- Does not use an LLM for summaries.
- Selects same-conversation turns deterministically by active topic/entity,
  investigation relevance, and recency, with strict turn, episode, and total budgets.
- Produces a storage-neutral `MemoryContextPackage` before final model context;
  fresh operational evidence remains authoritative over memory.

Typed long-term memory (Gate 6/7, disabled by default):

- Canonical records are atomic typed facts/outcomes: validated finding,
  investigation outcome, analyst correction, approved asset fact, known benign
  behavior, or hypothesis resolution. Assistant prose is never promoted
  automatically.
- `LongTermMemoryStore` owns create/get/revision-checked update, structured list,
  supersede, invalidate, and delete semantics. Local development uses the v4
  SQLite schema with user ownership and entity-link tables; SQLite is not a
  production memory database.
- Promotion is deterministic. Active authority requires analyst confirmation,
  trusted structured-source validation, or explicitly historical outcome status.
- The existing lazy BGE embedder and Qdrant adapter support a separate
  `soorin_copilot_memory_v1` collection. SQLite/Product remains canonical;
  Qdrant contains only an atomic retrieval projection and safe filter metadata.
- Retrieval combines owner-scoped exact entity lookup with dense candidates,
  canonical reload, validity/freshness policy, deduplication, and bounded Top-K.
  An optional local-only CrossEncoder reranks only the candidate pool and safely
  falls back to BGE order when disabled or unavailable.
- Long-term entries join the existing `MemoryContextPackage` under an independent
  token budget and carry type, epistemic status, freshness, provenance, and entity
  binding. Storage/index implementation names are not sent to synthesis.
- Retrieval occurs before semantic routing. Gate 8 consumes only typed,
  owner/entity-bound, authoritative records after TaskSpec. It may suppress a
  fully satisfied revision-based/historical call explicitly; volatile evidence
  refreshes and current operational evidence still outranks memory.
- Index failures never roll back canonical memory. Records expose explicit
  `pending`, `synced`, `stale`, `failed`, or `not_indexed` state, and reconciliation
  rebuilds from canonical records without startup-time indexing.

Routing state:

- Stores active IP, active entity pair, previous intent, previous scope, previous direction, previous depth, and previous operational provider state.
- Successful single-IP graph requests preserve active IP and node-summary route state.
- General or unclear detached turns do not erase active IP.
- Knowledge-only routes can be selected and included in trace/provider status, but current persisted `last_provider`/`last_providers` are operational-provider oriented and do not persist `knowledge` as a last provider.
- Stores safe Phase 2 continuity metadata: last plan ID, last review outcome, step evidence IDs, and capability statuses.
- Planner output and synthesizer prose cannot mutate routing state.

Local SQLite mode:

- `SOORIN_LOCAL_PRODUCT_SIMULATION_ENABLED=true` enables the owner-scoped local
  conversation/message repository.
- `SOORIN_THREAD_STATE_BACKEND=sqlite` enables compact thread continuity keyed by
  `conversation_id`, with legacy `session_id` fallback.
- `request_id` is the conversation-scoped turn idempotency key.
- State uses typed bounded JSON with schema versioning and optimistic revision;
  credentials, provider payloads, prompts, full EvidencePacks, runtime clients,
  locks, callbacks, and streams are never persisted.
- Storage failures are logged by safe error class and remain non-fatal to chat.
- This is local development/test infrastructure. Production ownership and durable
  transcripts remain a future Product PostgreSQL or Product API adapter concern.

### Unified internal Streamlit workspace

Gate 5 routes legacy and local-simulation chat through one `ChatBackend` contract,
`ConversationController`, chronological message renderer, and SSE event loop.
`LegacyDirectBackend` preserves the existing single-session developer workflow. With
`SOORIN_LOCAL_PRODUCT_SIMULATION_ENABLED=true` and
`SOORIN_STREAMLIT_AUTH_BACKEND=local_simulation`, Streamlit shows a password-free
local user selector, local chatrooms, and the existing topology workspace. It
calls protected `/local-simulation/*` routes for user and conversation metadata,
then calls the existing `/chat/stream` endpoint directly for every turn.

Each local conversation receives an opaque `conversation_id` and one stable
`session_id`. The conversation ID remains the durable thread key; the session ID
is reused as compatibility/runtime metadata. Completed user/assistant turns are
committed only by the existing SSE workflow and reloaded after `done`; Streamlit
does not write transcript messages itself. Local users are development metadata,
not Product users or authentication claims. OIDC is not configured. Working
summary, relevant turns, and episodes are restored through the same memory ports.
Product/PostgreSQL adapters and LangGraph checkpointing remain deferred. Typed
owner-scoped cross-conversation retrieval is available only when explicitly
enabled and a trusted `user_id` is present.

Each successful service request constructs one new `SessionRoutingState` and calls the state store once. Explicit-message, UI, and session entity authority remains owned by the resolver/router normalization path. General detached turns preserve useful active entity state. Safe-failure requests preserve prior active state unless the current request supplied a valid explicit or UI-authoritative investigation entity; Planner arguments and final prose are never state inputs.

## 16. Phase 2.1 Observability

Phase 3 adds node lifecycle events on the same allowlisted logging path:

- `langgraph_node_started`
- `langgraph_node_completed`
- `langgraph_node_failed`
- `langgraph_node_retried`
- `langgraph_node_skipped`
- specialist start/completion/failure/skip and specialist-node lifecycle events
- workflow start, completion, partial, and failure events

The detailed human workflow trace is adapted from final `InvestigationState`. It includes request/entity authority, routing, task/plan, evidence requirements, memory sufficiency/gap decisions, selected views, memory-skipped calls, `LANGGRAPH WORKFLOW`, `SPECIALISTS`, capability execution, evidence coverage, context/token budget, memory transition, bounded LLM-call counts, final status, and limitation reasons. It never renders prompts, model responses, credentials, memory statements, or evidence payloads.

Machine workflow events use compact allowlisted metadata with request, trace, session, plan, and step identifiers and support console or JSON formatting. They never contain complete prompts, full model responses, raw Product JSON, credentials, headers, or hidden reasoning.

Recorded timings include router, Planner, validation, per-capability, initial execution wall time, EvidencePack construction, review, supplemental retrieval, first streamed answer token, synthesis, and total request latency.

The human trace has two modes. `summary` retains the compact routing/plan/execution/review/synthesis/result view. `detailed` restores bounded section-by-section visibility for router input and state, entity authority, Planner source and latency, validated plan, capability results, Product evidence views, graph request completeness, Knowledge purpose/hash, evidence review, context inclusion, calibrated token budget, provider usage, snapshot result, state update, and final result. It never renders payloads, prompts, responses, credentials, headers, or reasoning. Unicode tree markers are used only on UTF-8 output. Color is automatic only for a TTY, respects `NO_COLOR`, and is disabled for redirected and JSON output.

Normal `app/run.py` startup configures terminal logging plus an optional UTF-8 `RotatingFileHandler` at `data/runtime/logs/soorin-copilot.log`. The default is a 20 MiB active file with 10 backups, approximately 200 MiB retained. File output strips ANSI and includes structured events and complete human trace blocks. Terminal/stdout remains authoritative for containers. Standard-library rotation targets the current single-process API; a future multi-worker deployment should aggregate stdout or use an external process-safe collector.

Optional request-scoped evidence snapshots are disabled by default and support `none`, shape-only `metadata`, safe actual-value `summary`, and deeper mandatory-`redacted` modes. Summary mode stores bounded task, plan, tool-result, review, and manifest values while excluding raw Product JSON, full model context, prompts, assistant responses, credentials, headers, and reasoning. Writes are atomic with `0700` root/date/request directories and `0600` files. Retention prunes complete request directories by age (48 hours), count (100), aggregate size (256 MiB), and per-request size (5 MiB), never removing the current write. Cleanup failures are non-fatal. Host storage is under ignored `data/runtime/evidence`; Compose overrides logs and snapshots to the existing persistent `/workspace/data/runtime/` API mount without adding a UI mount.

The event logger uses an allowlist and silently discards unknown fields. API keys, bearer tokens, Authorization headers, passwords, captcha values, complete Product payloads, prompts, full model responses, and hidden reasoning are not event fields. Existing system logs retain only bounded message/answer previews and safe deployment, status, count, latency, cache, freshness, completeness, and failure-class metadata.

Exact English/Persian greetings and thanks use a deterministic fast path that preserves non-streaming and SSE contracts while skipping entity resolution, Router, Planner, providers, and final LLM. It does not mutate operational routing state. Substantive greeting-prefixed messages use the normal workflow.

## 17. Streaming and Unicode

Streaming path:

- Arvan provider reads upstream SSE with `iter_lines(..., decode_unicode=True)` and UTF-8 response encoding.
- Provider emits typed `LLMStreamEvent` objects.
- API serializes SSE data with `json.dumps(..., ensure_ascii=False)`.
- API returns `StreamingResponse(..., media_type="text/event-stream; charset=utf-8")`.
- Streamlit uses `parse_sse_events` with an incremental UTF-8 decoder for byte chunks.

Covered Unicode cases:

- Smart double quotes.
- Smart single quotes.
- Em dash.
- Arrow.
- Persian text.
- Emoji.
- Split UTF-8 transport chunks.

Non-streaming chat remains unchanged and preserves Unicode text.

## 18. Streamlit UI

Implemented in `app/app_st.py` and `app/src/web`.

Current UI:

- Unified investigation workspace with sidebar logo.
- Sidebar status includes backend readiness, dynamic active LLM model from `/llm/health` with settings fallback, graph loaded status and node count, and selected topology target.
- Chat area uses `st.chat_message` and `st.chat_input`.
- Chat input is rendered after existing messages.
- Final answer streaming updates one assistant message and stores it once.
- UI context includes selected graph IP only when one is selected.
- Help content explains asset authority, evidence sources, example prompts, and precision tips.
- Topology page is embedded beside the chat area.
- Legacy and local-simulation modes share one backend protocol, controller,
  chronological chat renderer, and SSE event-processing path.

## 19. Deployment

Local launcher:

- `python app/run.py --api`
- `python app/run.py --web`

Docker:

- `Dockerfile` installs CPU-only Torch and application dependencies into `/opt/venv` in a builder stage, verifies no CUDA/NVIDIA packages, and runs as non-root `soorin`.
- Runtime copies only `/opt/venv`, `app`, and `lib`; local data, secrets, caches, wheels, and checkpoint files are excluded.
- Runtime exposes `6998` and `8501`.
- `compose.yaml` defines `api` and `ui` services.
- API uses `python app/run.py --api`.
- UI uses `python -m streamlit run app/app_st.py --server.port=8501`.
- Host port defaults are API `6998` and UI `8503`.
- Volumes:
  - One pre-existing host data root is bind-mounted at `/workspace/data`; API access is read/write and UI access is read-only.
  - The host Hugging Face cache is bind-mounted read-only into the API only.
  - Long bind syntax uses `create_host_path: false`, so missing or mistyped host paths fail instead of creating empty storage.

Local `app/.env` keeps repository-relative Graph and local Qdrant paths. Compose overrides only container-specific Qdrant, cache, log, and evidence paths. Host bind sources, image tag, restart policy, UID/GID, ports, and bind addresses are configured in untracked `compose.env`. The UI does not receive `app/.env`, the original source corpus, model cache, or backend credentials.

Normal retrieval reads indexed Qdrant payloads and does not open original corpus files. `SOORIN_RAG_SOURCE_ROOT` is therefore an indexing-maintenance input, and the source corpus is not mounted during normal API/UI operation. Embedded Qdrant is opened only by the API process.

Compose currently does not define a separate Qdrant server container. Use local Qdrant mode for embedded file-backed Qdrant storage, or configure an external Qdrant server URL.

## 20. Tests

Current focused tests:

- `test_agentic_rag_foundation.py`: contracts, registry, fake embedder/vector store, Qdrant config/unavailable state, knowledge.search states, task mapping, reviewer, RAG context.
- `test_chat_streaming.py`: provider streaming, SSE contract, UTF-8, non-streaming preservation, UI contract checks.
- `test_context_budget_allocation.py`: context budget and graph/product/knowledge inclusion behavior.
- `test_context_routing.py`: semantic/fallback routing, entity authority, active single/pair follow-ups, graph scopes, UI authority, subnet formatting, topology UI helpers.
- `test_detection_integration.py`: product JSON provider and detection/profile behavior.
- `test_detection_phase12.py`: additional detection/profile/cache/context protections.
- `test_llm_deployments.py`: multi-deployment settings and LLM health.
- `test_llm_retry.py`: transient retry and non-retry behavior.
- `test_phase2_agent_workflow.py`: plan validation, planner JSON/repair boundaries, DAG execution, concurrency, cancellation, safe failures, raw payload preservation, EvidencePack/reviewer behavior, event safety, and typed workflow dispatch.

Common commands:

```bash
PYTHONPATH=app python -m compileall -q app
PYTHONPATH=app python -m pytest app/src/tests/test_agentic_rag_foundation.py -q
PYTHONPATH=app python -m pytest app/src/tests/test_chat_streaming.py -q
PYTHONPATH=app python -m pytest app/src/tests/test_phase2_agent_workflow.py -q
PYTHONPATH=app python -m unittest app.src.tests.test_context_routing -v
git diff --check
```

Live Product API, Arvan, embedding downloads, Qdrant server, graph refresh, and indexing should be tested only deliberately, never as default offline verification.

## 21. Implemented, Partial, Deferred

Implemented:

- FastAPI chat and graph APIs.
- Streamlit workspace with topology UI and streaming chat.
- Provider-neutral LLM client with OpenAI-compatible Kimi/GLM/GPT deployment aliases.
- Semantic LLM router with deterministic validation and fallback.
- Deterministic IPv4 entity authority.
- Product auth/client for topology, detection, profile, and login.
- NetworkX graph build, storage, refresh, retrieval, and visualization.
- Optional Qdrant-backed RAG foundation.
- BGE embedding configuration and lazy Hugging Face embedder.
- Context composer with provider coverage, budgets, and limitations.
- In-memory conversation and routing state.
- Disabled-by-default typed long-term memory with local SQLite canonical storage,
  separate Qdrant/BGE retrieval, and optional bounded reranking.
- UTF-8-safe SSE streaming.
- Bounded typed agent contracts, registry, task mapping, and reviewer foundation.
- Active deterministic direct-plan compiler and bounded multi-step Planner.
- Deterministic plan validation and dependency-aware capability execution.
- Canonical ToolResult conversion and reviewed EvidencePack synthesis source.
- Two-stage deterministic review, one-supplemental retrieval policy, and deterministic safe failure.
- Request/trace/plan/step workflow observability.

Partial or structural:

- RAG has service and indexer support, but no dedicated public API endpoint and no automatic index build.
- Specialist findings are deterministic evidence-availability summaries; cross-domain interpretation remains with the parent reviewer and final synthesizer.

Deferred or not implemented:

- LLM evidence reviewer.
- Neo4j, GraphStore migration, Cypher, text-to-Cypher, Graph Data Science.
- GraphRAG and bulk graph enrichment.
- MCP.
- SIEM/Splunk integrations.
- Alert actions.
- Report-generation endpoints.
- Durable cross-process episodic conversation memory.
- Human approval workflows.
- Automatic remediation.
- Microsoft Global or DRIFT GraphRAG.

## 22. Remaining Risks and Next Step

Remaining risks:

- There is no durable workflow checkpointing; in-flight work cannot resume after a process restart.
- Synchronous provider calls cannot be killed after a Python future timeout; transport-native timeouts must remain correctly configured.
- Planner mode is offline-tested with fakes but requires deliberate manual parity testing before broad enablement.
- Raw conversation/episode memory remains process-local. Optional local SQLite
  continuity does not provide production tenant authorization or multi-replica
  coordination.
- RAG availability and freshness depend on an externally maintained Qdrant collection; the application does not index at startup.

The active upgrade roadmap is: validate Gate 6/7 retrieval against local approved
memory, then add Gate 8 freshness/evidence-gap decisions without weakening live
evidence authority; after that, introduce an Organization Intelligence Plane that
remains subordinate to live Product and Graph evidence. Neo4j, GraphRAG, Planner
expansion, MCP/vendor tools, bulk enrichment, and side-effecting actions remain
separate deferred capabilities.
