# Soorin Cyber Copilot — Deep Project Analysis & OpenCode Platform Guide

> Generated: 2026-07-23 | Branch: dev | Repository root: Copilot/

---

## Part A — About This Report

This document covers two topics:

1. **OpenCode** — the AI coding agent platform powering this session: what it is, its free model, limitations, features, supported interfaces, and how to configure custom third-party API providers.
2. **Soorin Cyber Copilot** — a deep analytical report of the entire project: architecture, workflow, components, data flow, strengths, gaps, and risk areas.

No code was modified during this analysis. All source material was read from `docs/`, project configuration files, directory listings, and the OpenCode public documentation.

---

## Part B — OpenCode Platform Overview

### B.1 What Is OpenCode?

OpenCode is an **open-source AI coding agent** built by Anomaly. It helps developers write, debug, refactor, and understand code across terminals, IDEs, and desktop applications. It is not a closed product — its source is on GitHub with 160K+ stars, 900+ contributors, and 7.5M monthly active developers.

**Core idea:** You give it a natural-language prompt; it reads your codebase, plans a solution, and makes changes using built-in tools (file read/write, bash execution, search, git operations, etc.).

### B.2 The Free Model — OpenCode Zen

OpenCode offers **OpenCode Zen**, a curated set of models tested and benchmarked specifically for coding agents:

| Aspect | Details |
|---|---|
| What it is | A managed provider with a hand-picked set of validated models |
| Access | Sign up at opencode.ai/auth, add billing, get an API key |
| Free tier | OpenCode itself is free and open-source; Zen models are pay-per-use through OpenCode's billing |
| Alternative free options | Use any free-tier provider (Ollama local, LM Studio local, NVIDIA build.nvidia.com free tier, Hugging Face Inference) |

**OpenCode Go** is a low-cost subscription plan for open coding models, also managed by the OpenCode team.

**Important:** OpenCode is the *agent platform*. Models come from providers. There is no single "free model" bundled — you choose your provider.

### B.3 Limitations

| Category | Limitation |
|---|---|
| Token limits | Depends entirely on your chosen model/provider (e.g., Claude Sonnet: 200K context; GPT-4o: 128K; local models vary) |
| Time limits | No platform-enforced time limit; session timeout depends on your terminal/IDE |
| Rate limits | Provider-specific (Zen has tested rates; free tiers like NVIDIA may have lower limits) |
| Storage | Conversations are local unless you use `/share` to generate a share link |
| Context window | Configurable per-session; OpenCode manages compaction automatically |
| Sandbox | No built-in sandbox — runs on your machine with your permissions |
| Privacy | OpenCode does not store your code; context stays local |

### B.4 All OpenCode Features and Abilities

#### Interfaces

| Interface | Available | Description |
|---|---|---|
| **TUI (Terminal)** | Yes | Primary interface in any modern terminal (WezTerm, Alacritty, Ghostty, Kitty) |
| **CLI** | Yes | Headless scripting and automation |
| **Desktop App** | Yes | macOS, Windows, Linux with tabs |
| **VS Code Extension** | Yes | IDE integration |
| **Web** | Yes | Browser-based interface |

#### Tools (Built-in)

| Tool | Function |
|---|---|
| **Read** | Read files with line numbers, offset/limit, images, PDFs |
| **Write** | Create or overwrite files |
| **Edit** | Exact string replacement in files with context-aware edits |
| **Bash** | Execute shell commands (git, npm, docker, pytest, etc.) |
| **Glob** | File pattern matching (`**/*.tsx`, `src/**/*.ts`) |
| **Grep** | Fast content search with regex across the codebase |
| **Task** | Delegate complex multi-step work to sub-agents |
| **WebFetch** | Retrieve and analyze web content (docs, APIs) |
| **WebSearch** | Real-time web search with live crawling |
| **Question** | Ask user questions during execution |
| **TodoWrite** | Track multi-step task progress |

#### Git & GitHub Tools

| Feature | Details |
|---|---|
| Git operations | Full git support (status, diff, commit, branch, merge, rebase, push, pull, stash) |
| GitHub integration | PR creation, issue management, check status, releases via `gh` CLI |
| Safe workflows | Branch switching, conflict resolution, stash handling with safety guards |
| GitLab integration | Experimental support for GitLab Duo Agent Platform |

#### Development Features

| Feature | Description |
|---|---|
| LSP integration | Automatic language server loading for the LLM |
| Multi-session | Multiple agents in parallel on the same project |
| Share links | Generate shareable conversation links |
| Plan Mode | Disable editing to review plans before execution |
| Undo/Redo | `/undo` and `/redo` commands for reversible changes |
| Custom commands | Define project-specific slash commands |
| Agent Skills | Load specialized skills for specific domains |
| MCP servers | Model Context Protocol server integration |
| ACP support | Agent Communication Protocol |
| Custom tools | Define your own tools |
| Plugins | Extend functionality via npm packages or local plugins |
| Themes | Customizable UI themes |
| Keybinds | Configurable keyboard shortcuts |
| Formatters | Code formatting integration |
| Permissions | Granular per-tool, per-pattern allow/ask/deny rules |
| References | Make external directories and Git repos available as context |
| Policies | Enterprise-grade policy controls |

### B.5 How OpenCode Works

1. **Install:** `curl -fsSL https://opencode.ai/install | bash` or via npm/brew
2. **Configure:** Set API keys via `/connect` command or `opencode.json`
3. **Initialize:** Run `opencode` in your project, then `/init` to generate `AGENTS.md`
4. **Use:** Ask questions, request changes, review plans, switch between Plan/Build modes
5. **Customize:** Agents, skills, commands, MCP servers, permissions via `opencode.json`

**Config file:** `opencode.json` (project root) or `~/.config/opencode/opencode.json` (global). Always declare `"$schema": "https://opencode.ai/config.json"` for editor validation.

### B.6 Custom Third-Party API Provider (OpenAI-Compatible)

Any provider exposing an OpenAI-compatible `/v1/chat/completions` endpoint can be used. Configuration in `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "my-custom-provider": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "My Custom LLM",
      "options": {
        "baseURL": "https://my-api.example.com/v1",
        "apiKey": "sk-..."
      },
      "models": {
        "my-model-id": {
          "name": "My Model Display Name",
          "limit": {
            "context": 32768,
            "output": 4096
          }
        }
      }
    }
  },
  "model": "my-custom-provider/my-model-id"
}
```

**Key fields:**

| Field | Purpose |
|---|---|
| `npm` | Use `@ai-sdk/openai-compatible` for any OpenAI-compatible API |
| `options.baseURL` | Your API endpoint (must expose `/v1/chat/completions`) |
| `options.apiKey` | Your API key |
| `models.<id>.name` | Display name in the UI |
| `models.<id>.limit.context` | Context window size |
| `models.<id>.limit.output` | Max output tokens |
| `provider` (top-level) | Select default provider with `"model": "provider/model-id"` |

**Also works for:**
- Local models: Ollama (`localhost:11434/v1`), LM Studio (`localhost:1234/v1`), llama.cpp (`localhost:8080/v1`)
- Proxies: Helicone, Cloudflare AI Gateway, LLM Gateway
- Any self-hosted OpenAI-compatible server

**Credential storage:** `~/.local/share/opencode/auth.json` (managed by `/connect`).

---

## Part C — Soorin Cyber Copilot: Deep Analytical Report

### C.1 Project Identity

| Attribute | Value |
|---|---|
| Name | Soorin Cyber Copilot |
| Purpose | Bounded cybersecurity investigation assistant for SOC/NOC and asset-intelligence workflows |
| Domain | Network security, asset intelligence, threat investigation |
| Technology | Python 3.12, FastAPI, Streamlit, LangGraph, NetworkX, Qdrant, Docker |
| Architecture pattern | Evidence-aware agentic workflow with deterministic constraints |

### C.2 What It Does

The Copilot answers cybersecurity questions by combining:

1. **Live operational evidence** from the Soorin product API (asset profiles, detection data)
2. **Network topology** from a directed graph of observed IP communication pairs
3. **Knowledge/RAG** from approved SOC documentation (runbooks, MITRE references, protocols)
4. **Session memory** with conversation continuity and entity tracking
5. **LLM synthesis** — but the model never invents operational facts

**Core principle:** Deterministic systems own identity, evidence, and safety. The LLM is a semantic router and synthesis engine, not an authority on operational data.

### C.3 Architecture Overview

```
Streamlit UI  ──>  FastAPI API  ──>  CopilotService Facade  ──>  LangGraph Workflow
                                                                    │
                                    ┌───────────────────────────────┤
                                    │                               │
                              Entity Resolution              Semantic Router
                                    │                               │
                              Task Validation              Plan Validation
                                    │                               │
                              Capability Execution    ◄──── Capability Registry
                              │  Asset Specialist
                              │  Graph Specialist
                              │  Generic Knowledge
                                    │
                              EvidencePack Builder
                                    │
                              Evidence Reviewer (deterministic)
                                    │
                              Context Composer (budget-aware)
                                    │
                              Final LLM Synthesis (configured Kimi/GLM/GPT)
                                    │
                              Memory Update
```

### C.4 Component Deep Dive

#### C.4.1 API Layer (`app/src/api/`)

| File | Responsibility |
|---|---|
| `main.py` | FastAPI app creation, router inclusion, LLM validation, graph loading, refresh scheduler |
| `routes.py` | `/chat`, `/chat/stream` — request handling, service invocation, SSE streaming |
| `graph_routes.py` | `/graph/status`, `/graph/stats`, `/graph/nodes/{ip}`, neighbors, context, path |
| `schemas/` | Pydantic request/response models |
| `dependencies.py` | Dependency injection for shared services |

**Endpoints:**

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/llm/health` | LLM provider readiness (no generation call) |
| POST | `/chat` | Non-streaming chat envelope |
| POST | `/chat/stream` | SSE streaming final answer |
| GET | `/graph/status` | Active graph status and refresh metadata |
| GET | `/graph/stats` | Graph statistics |
| GET | `/graph/nodes/{ip}` | Single node degree lookup |
| GET | `/graph/nodes/{ip}/neighbors` | Direct neighbors |
| GET | `/graph/nodes/{ip}/context` | Deterministic context digest |
| GET | `/graph/path` | Shortest communication path |

#### C.4.2 Configuration (`app/src/config/`)

| File | Purpose |
|---|---|
| `settings.py` | All configuration from `app/.env` + environment variables, validated at startup |
| `llm_deployments.py` | OpenAI-compatible deployments: `kimi`, `glm`, and retained `gpt55` |

**Major configuration groups:**

- **API/UI:** `API_HOST`, `API_PORT`, `API_RELOAD`, `STREAMLIT_SERVER_PORT`
- **LLM:** Router/chat deployments, base URLs, models, API keys, timeouts, token limits, sampling
- **Product API:** Base URL, topology/profile/detection/login paths, credentials, HWID, retry
- **Graph:** Artifact paths, UI limits, retrieval caps, context budgets, refresh policy
- **RAG:** Enable flag, source root, Qdrant mode/server, collection, BGE model/dimension
- **Memory:** History limits, summary settings
- **Observability:** Log format, color, trace detail, evidence snapshots

#### C.4.3 Core Agent Workflow (`app/src/core/agent/`)

This is the brain of the system — a 13-node bounded LangGraph workflow.

| File | Symbol | Role |
|---|---|---|
| `workflow.py` | `BoundedCopilotWorkflow` | Bounded no-checkpointer LangGraph lifecycle with conditional edges |
| `nodes.py` | `CopilotWorkflowNodes` | All 13 node implementations (request-scoped) |
| `contracts.py` | `InvestigationState`, `TaskSpec`, `ExecutionPlan`, `ToolResult`, `EvidenceFact`, `EvidencePack`, `ReviewDecision`, `CapabilitySpec` | Typed contracts |
| `planner.py` | `BoundedPlanner` | Optional LLM-based multi-step planner |
| `plan_validator.py` | `PlanValidator` | Deterministic capability/entity/budget/DAG enforcement |
| `registry.py` | `build_capability_registry` | 8 registered capabilities with provider adapters |
| `executor.py` | `CapabilityExecutor` | Dependency-aware concurrent execution (max 4 workers) |
| `reviewer.py` | `EvidenceReviewer` | Two-stage deterministic review (retrieval + context) |
| `evidence.py` | `EvidencePack` builder, `apply_context_inclusion` | Evidence packaging and composer integration |
| `task_mapping.py` | Task mapping | Route decision to TaskSpec translation |
| `events.py` | Event types | Typed lifecycle events for observability |
| `context_identity.py` | Context identity | Identity keys for evidence tracking |
| `specialists/` | Asset Investigation Specialist, Graph Analysis Specialist | Typed bounded LangGraph subgraphs (zero-LLM) |

**The 13-Node Workflow:**

```
resolve_entities -> route -> validate_task -> build_direct_plan OR build_plan
-> validate_plan -> dispatch_specialists -> join_specialist_results
-> build_evidence -> review_retrieval -> (supplemental_retrieval)
-> compose_context -> review_context -> synthesize -> update_memory
```

**Registered Capabilities (8):**

| Capability | Source |
|---|---|
| `asset.get_profile` | Product API profile endpoint |
| `asset.get_detection` | Product API detection endpoint |
| `graph.get_summary` | NetworkX node summary |
| `graph.get_neighbors` | NetworkX direct/2-hop neighbors |
| `graph.get_relationship` | NetworkX edge check |
| `graph.compare_assets` | NetworkX pair comparison |
| `graph.find_path` | NetworkX shortest path |
| `knowledge.search` | Qdrant vector search |

**Hard limits (architectural bounds):**

| Parameter | Value |
|---|---|
| Max capability calls per request | 6 |
| Max entities per request | 2 |
| Max graph depth | 2 hops |
| Max supplemental retrievals | 1 |
| Max executor concurrency | 4 workers |
| LangGraph recursion limit | 32 |
| Plan calls to Planner | 1 (fixed) |

#### C.4.4 Context & Routing (`app/src/core/context/`)

| File | Purpose |
|---|---|
| `entities.py` | Deterministic IPv4 entity extraction with authority order |
| `intent.py` | Semantic LLM router (configured Kimi/GLM/GPT deployment) |
| `router.py` | Deterministic fallback router (regex-based, after LLM failure) |
| `composer.py` | Context composition with provider coverage, budgets, and limitations |
| `product_views.py` | Product evidence view selection and inventory |
| `models.py` | Context model types |
| `providers/` | Product JSON provider, graph provider adapters |

**Entity Authority Order:**

```
explicit IP in current message > selected UI IP > active session IP/entities
```

**Semantic Routing Flow:**

```
deterministic entity extraction -> semantic LLM router -> strict schema validation
-> deterministic normalization -> deterministic fallback only on failure
```

**Router output fields:** intent, scope, direction, depth, requires_graph, requires_detection, requires_asset_profile, requires_knowledge, entity_binding, requires_multiple_entities, is_followup, classification_confidence, reason.

#### C.4.5 Graph Topology (`app/src/core/graph/`)

| File | Purpose |
|---|---|
| `builder.py` | Builds directed NetworkX DiGraph from Product topology |
| `loader.py` | Loads active graph and provides `get_graph()` |
| `retrieval.py` | Scope-specific graph retrieval (6 scopes) |
| `refresh.py` | Background auto-refresh with validation thresholds |
| `storage.py` | Artifact persistence (pickle, JSON, GraphML, GEXF) |
| `build_service.py` | Orchestrates graph build/refresh |
| `service.py` | Graph query service |
| `context.py` | Graph context generation |
| `visualization.py` | PyVis/vis-network interactive graph generation |

**Retrieval Scopes:**

| Scope | Use Case |
|---|---|
| `node_summary` | Single node summary (degree, subnet, importance — no peer identities) |
| `one_hop` | Bounded direct neighbors |
| `full_neighbors` | Exhaustive direct-neighbor retrieval within hard max |
| `two_hop` | Neighbors of neighbors |
| `multi_entity_comparison` | Pair comparison with shared/distinct peers |
| `path` | Shortest observed communication path |

**Graph Context Token Caps:**

| Scope | Cap |
|---|---|
| node_summary | 700 tokens |
| relationship | 700 tokens |
| path | 1,200 tokens |
| one_hop | 1,800 tokens |
| comparison | 2,200 tokens |
| two_hop | 2,500 tokens |
| full_neighbors | 3,000 tokens |

#### C.4.6 RAG & Knowledge Search (`app/src/core/rag/`)

| File | Purpose |
|---|---|
| `sources.py` | External corpus discovery and loading |
| `chunker.py` | Deterministic character chunking with content-based UUID chunk IDs |
| `embeddings.py` | Lazy Hugging Face embedder (BAAI/bge-base-en-v1.5, 768-dim) |
| `vector_store.py` | Soorin-owned VectorStore protocol |
| `qdrant_store.py` | Qdrant implementation (server or local mode) |
| `service.py` | `knowledge.search` orchestration |
| `citations.py` | Citation creation |
| `safety.py` | Prompt-injection detection on retrieved chunks |
| `indexer.py` | Explicit validation/indexing entry point |
| `models.py` | RAG model types |

**Default embedding model:** `BAAI/bge-base-en-v1.5` — attention-mask-aware mean pooling, L2 normalization, strict `local_files_only` mode.

**RAG statuses:** `ok`, `empty`, `not_configured`, `unavailable`, `invalid`, `partial`.

**Key constraint:** RAG is documentation evidence only. It never overrides current Product or Graph evidence.

#### C.4.7 LLM Client (`app/src/core/llm/`)

| File | Purpose |
|---|---|
| `client.py` | Provider-neutral LLM client |
| `providers/arvan.py` | Arvan-compatible OpenAI-protocol deployment |
| `errors.py` | Typed LLM errors |
| `output_parser.py` | Response content extraction |
| `token_estimator.py` | Token estimation with configurable multiplier |

**Deployments:**

| Alias | Default Model | Purpose |
|---|---|---|
| `kimi` | kimi-k3 | Intent router and chat synthesis (default) |
| `glm` | GLM-5.2 | Planner (default when enabled) |
| `gpt55` | GPT-5.5 | Retained configurable compatibility deployment |

Both use OpenAI-compatible `messages` format, `Authorization: <scheme> <key>`, and extract `choices[0].message.content`.

#### C.4.8 Memory (`app/src/core/memory/`)

| File | Purpose |
|---|---|
| `store.py` | In-memory conversation and routing state store |
| `episodes.py` | Working Memory and Episodic Session Memory |
| `routing_state.py` | Active IP, pair, previous route/plan state |

**Features:**
- Per-process in-memory state (no cross-worker coordination)
- Deterministic compact summaries when token thresholds exceeded
- Topic detachment on entity/pair/topic changes
- Active episode records without treating them as current evidence
- No LLM used for summaries

#### C.4.9 Observability (`app/src/core/observability/`)

| File | Purpose |
|---|---|
| `logging.py` | Structured logging with terminal/file output |
| `llm_usage.py` | Usage recording and reporting |
| `snapshots.py` | Optional request-scoped evidence snapshots |

**Features:**
- Human-readable trace (summary or detailed mode)
- Machine workflow events with allowlisted metadata
- Rotating UTF-8 file logs (20 MiB active, 10 backups)
- Optional evidence snapshots (metadata/summary/redacted modes)
- TTY-aware color, NO_COLOR support

#### C.4.10 Streamlit UI (`app/src/web/` and `app/app_st.py`)

| File | Purpose |
|---|---|
| `app_st.py` | Main Streamlit entry point |
| `chat_stream.py` | SSE stream parsing and incremental rendering |
| `components/` | Reusable UI components |
| `copilot_help.py` | Help content explaining evidence authority and tips |
| `pages/` | Topology page with interactive graph |

**Features:**
- Unified investigation workspace with sidebar status
- Chat with streaming final answers
- Interactive PyVis/vis-network topology explorer
- Node-click selection for Copilot context
- Subnet filters, degree filters, physics-mode controls
- Session status: backend health, active LLM model, graph state

### C.5 Evidence Lifecycle — End to End

```
1. User question arrives at POST /chat
2. CopilotService creates request_id, trace_id, session context
3. BoundedCopilotWorkflow starts
4. resolve_entities: deterministic IPv4 extraction with authority ordering
5. route: LLM classifies intent/scope/requires_* with schema validation
6. validate_task: rejects too many entities, incomplete comparisons
7. build_direct_plan (or build_plan if multi-step + planner enabled)
8. validate_plan: enforces capability registry, entity cardinality, DAG, budgets
9. dispatch_specialists: Asset Specialist (profile/detection), Graph Specialist (graph.*)
10. join_specialist_results: restore parent plan step order
11. build_evidence: pack ToolResults into EvidencePack
12. review_retrieval: deterministic sufficiency check
13. supplemental_retrieval: at most one extra capability if needed
14. compose_context: budget-aware provider manifest + tagged JSON sections
15. review_context: verify model window safety
16. synthesize: final LLM call with bounded messages
17. update_memory: store active entities, completed turn, routing state
18. Response returned as envelope or SSE stream
```

### C.6 Data Flow Diagram

```
                         ┌──────────────────────┐
                         │   Soorin Product API  │
                         │  (Profile, Detection, │
                         │   Topology, Login)    │
                         └──────────┬───────────┘
                                    │ HTTP
                         ┌──────────▼───────────┐
                         │  ProductApiClient     │
                         │  auth + retry + cache │
                         └──────────┬───────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
    ┌─────────▼─────────┐ ┌────────▼────────┐ ┌─────────▼─────────┐
    │ Profile Provider   │ │ Detection       │ │ Topology Builder   │
    │ Detection Provider │ │ Provider        │ │ NetworkX Graph     │
    └─────────┬─────────┘ └────────┬────────┘ └─────────┬─────────┘
              │                     │                     │
              │                     │                     │
    ┌─────────▼─────────────────────▼─────────────────────▼─────────┐
    │                   Capability Registry                          │
    │  asset.get_profile | asset.get_detection | graph.* | knowledge │
    └─────────────────────────────┬───────────────────────────────────┘
                                  │ ToolResult
                                  ▼
    ┌─────────────────────────────────────────────────────────────────┐
    │                      EvidencePack                               │
    └─────────────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
    ┌─────────────────────────────────────────────────────────────────┐
    │                    ContextComposer                              │
    │  Provider Manifest + Product JSON + Graph JSON + Knowledge JSON │
    │  Budget: Product-first > Graph > Knowledge                      │
    └─────────────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
    ┌─────────────────────────────────────────────────────────────────┐
    │                  Final LLM Synthesis                            │
    │  System prompt + Dynamic context + History + User request       │
    └─────────────────────────────┬───────────────────────────────────┘
                                  │
                    ┌─────────────┼──────────────┐
                    ▼                            ▼
              Streaming SSE              Non-streaming JSON
```

### C.7 Key Architectural Principles

| Principle | Implementation |
|---|---|
| **Deterministic systems own authority** | Entity resolution, capability selection, plan validation, graph retrieval, context budgeting, state updates — all deterministic |
| **LLM is router + synthesizer, not authority** | Router classifies intent; synthesizer generates natural language; neither invents operational facts |
| **Current evidence outranks prose** | Product evidence and graph evidence outrank any previous LLM-generated answer |
| **Entity authority ordering** | Explicit message > UI selection > session state |
| **Topic detachment** | General questions do not inherit stale asset context |
| **Provider failures are safe** | Represented as unavailable/partial/stale, never converted to verified facts |
| **Graph = topology evidence only** | Does not prove routing, trust, dependency, or reachability |
| **RAG = documentation evidence only** | Does not override Product or Graph evidence |
| **No unrestricted tool loop** | Every capability is registered, validated, budgeted, and reviewed |

### C.8 Test Coverage

| Test File | Coverage Area |
|---|---|
| `test_phase2_agent_workflow.py` | Plan validation, planner boundaries, DAG execution, concurrency, cancellation, EvidencePack, reviewer |
| `test_phase3_langgraph_workflow.py` | LangGraph workflow lifecycle |
| `test_phase3_specialists.py` | Specialist subgraph behavior |
| `test_phase21_stabilization.py` | Phase 2.1 stabilization |
| `test_agentic_rag_foundation.py` | RAG contracts, registry, Qdrant states, knowledge.search |
| `test_chat_streaming.py` | Provider streaming, SSE contract, UTF-8 |
| `test_context_budget_allocation.py` | Context budget and inclusion behavior |
| `test_context_routing.py` | Semantic/fallback routing, entity authority |
| `test_detection_integration.py` | Product JSON provider behavior |
| `test_detection_phase12.py` | Detection/profile/cache protections |
| `test_llm_deployments.py` | Multi-deployment settings and health |
| `test_llm_retry.py` | Transient retry behavior |
| `test_llm_usage.py` | Usage recording |
| `test_observability.py` | Logging and trace |

**Testing philosophy:** Offline tests use fakes/mocks. No live Product API, Arvan, Qdrant, FastAPI, Streamlit, Docker, indexing, or embedding calls in unit tests.

### C.9 Deployment

| Method | Details |
|---|---|
| Local | `python app/run.py --api` (port 6998), `python app/run.py --web` (port 8503) |
| Docker | Multi-stage build, non-root `soorin` user, 6998 + 8501 exposed |
| Compose | Two services: `api` (FastAPI) and `ui` (Streamlit) |
| Volumes | `copilot-data` (graph/runtime), `copilot-qdrant` (vector storage) |
| Read-only mounts | SOC corpus + Hugging Face cache mounted into API only |
| Health checks | API: `/health` + `/openapi.json`; UI: `/_stcore/health` |

### C.10 Strengths

| Strength | Evidence |
|---|---|
| **Architectural rigor** | 778-line architecture document, evidence lifecycle audit, typed contracts everywhere |
| **Deterministic safety** | LLM never invents operational facts; capability registry enforces known operations only |
| **Evidence traceability** | Full lineage from provider fetch → ToolResult → EvidencePack → Context → Model |
| **Budget awareness** | Token-level context budgeting with Product-first priority, safety margins, and hard guards |
| **Two-stage review** | Retrieval review + context review prevent unsafe synthesis |
| **Bounded execution** | LangGraph runs without checkpoint persistence; state and memory remain process-local |
| **Observability** | Human trace + machine events, evidence snapshots, structured logging |
| **Clean separation** | CopilotService (facade) → Workflow (orchestration) → Nodes (implementation) |
| **Specialist pattern** | Domain-specific bounded subgraphs without additional LLM calls |
| **RAG safety** | Prompt-injection filtering, score thresholds, status classifications |

### C.11 Known Gaps and Risks

| Priority | Issue | Impact |
|---|---|---|
| P0 (resolved) | Graph comparison could lose direct relationship | Patched with protected fact block |
| P1 | Product false-negative after composition | Identity mismatch in `apply_context_inclusion` can exclude valid evidence |
| P1 | `completed_with_limitations` over-broad | Normal caveats (Knowledge Top-K, graph limits) downgrade usable answers |
| P1 | Product-first budget can exclude Graph | Large Product payloads consume budget before Graph context |
| P2 | Human trace inaccuracies | Duplicate sections, missing node records |
| P2 | Process-local memory | No cross-worker state coordination |
| P3 | Knowledge provider label leak | Partially patched; prompt attribution still evolving |

### C.12 Deferred / Not Implemented

| Area | Status |
|---|---|
| LLM evidence reviewer | Not implemented (deterministic only) |
| Neo4j / GraphStore / Cypher | Deferred |
| GraphRAG / bulk enrichment | Deferred |
| MCP integration | Deferred |
| SIEM/Splunk integration | Deferred |
| Alert actions / remediation | Deferred |
| Report generation endpoints | Deferred |
| Long-term durable memory | Deferred |
| Human approval workflows | Deferred |
| Automatic remediation | Deferred |

### C.13 Recommended Next Steps

1. **Acceptance testing** — Offline parity testing of the stabilized parent/specialist workflow before new features
2. **Product inclusion hardening** — Fix identity mismatch in `apply_context_inclusion` to prevent false-negative evidence exclusion
3. **Reviewer caveat separation** — Distinguish material evidence limitations from normal provider caveats
4. **Graph budget rebalancing** — Ensure required Graph context can survive large Product payloads
5. **Neo4j foundation** — Behind existing graph capability boundary with bounded parameterized read-only queries
6. **Cross-process memory** — Move from process-local to shared state for multi-worker deployment
7. **Planner enablement testing** — Manual parity testing before broad `SOORIN_PLANNER_ENABLED=true`

---

## Part C Summary Table

| Dimension | Status |
|---|---|
| Architecture | Mature, well-documented, principle-driven |
| Core workflow | 13-node LangGraph with typed contracts, deterministic safety |
| Product integration | Working (profile, detection, topology, auth) |
| Graph | Working (build, refresh, 6 retrieval scopes, visualization) |
| RAG | Foundation complete; indexing external; no auto-build |
| LLM | Arvan-compatible, dual deployment, streaming, retry |
| Observability | Comprehensive (human trace, machine events, snapshots) |
| Tests | 14 focused test files covering contracts, routing, workflow, streaming |
| Deployment | Docker Compose with health checks, security hardening |
| Known issues | 4 open items (P1-P3) with identified fixes |
| Deferred work | 10 major areas listed for future phases |

---

*End of report.*
