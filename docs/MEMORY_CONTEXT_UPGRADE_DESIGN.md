# Soorin Copilot Memory and Context Upgrade Design

**Status:** target architecture plus verified Gate 3 local-persistence slice.
Gate 3 implements disabled-by-default local SQLite `ChatRepository` and compact
`ThreadStateStore` adapters. Trusted Product identity/authorization, production
Product persistence, long-term memory, semantic memory, context compaction, and
LangGraph checkpointing remain future work.

**Source audit:** 2026-08-05, branch `dev`, baseline HEAD `f9a62c6` plus the
verified Gate 3 working tree. Claims labelled “current” were verified against
repository source, tests, configuration, or the current Product integration
contract. No services or external providers were run.

## 1. Executive summary

Copilot already has a bounded, typed evidence workflow: deterministic entity
resolution, semantic routing, validated direct or planned execution, typed
`ToolResult`/`EvidencePack` review, bounded context composition, and final
synthesis. It also has process-local conversation history, working memory,
episodic summaries, and routing state.

The next problem is not “add a vector database for chat history.” The missing
architecture is durable, tenant-scoped ownership for thread state and validated
organizational facts. The safe upgrade is to introduce ports for Product-owned
chat records, Copilot-owned thread state, typed durable memory, and a semantic
index. Live Product and Graph evidence stay authoritative; memory reduces repeat
work but does not replace a required fresh lookup.

## 2. Verified current architecture

### 2.1 Implemented request pipeline

```text
POST /chat or /chat/stream
-> CopilotService prepares request/session/trace runtime
-> bounded LangGraph workflow
-> resolve entities -> semantic route -> validate TaskSpec
-> direct deterministic plan or one bounded Planner proposal
-> validate/fallback plan -> registered read-only capabilities
-> ToolResults -> EvidencePack -> deterministic reviewer
-> ContextComposer -> final chat model -> update in-process memory
```

Direct tasks skip the Planner. The Planner is a bounded proposal mechanism,
not a tool loop. The registry currently wraps Profile, Detection, NetworkX
Graph, and `knowledge.search`.

### 2.2 Current contracts and authority

The public chat request accepts `message`, optional `session_id`, and optional
`ui_context.selected_ip`. The response/SSE contracts do not expose request or
trace IDs. Entity authority is implemented as:

```text
explicit entity in this message > selected UI entity > active session entity
```

General detached questions are intentionally prevented from inheriting stale
asset context. The semantic LLM router is normal decision source; deterministic
logic validates, normalizes, and safely falls back only when needed.

### 2.3 Current evidence capabilities

| Capability family | Current source | Current authority |
| --- | --- | --- |
| Asset Profile | Product API via shared client | point-in-time operational evidence |
| Asset Detection | Product API via shared client | point-in-time operational evidence |
| Graph | validated NetworkX last-known-good graph | observed topology snapshot |
| Knowledge | optional local/server Qdrant RAG | documentation only |

`ToolResult` preserves status, entity binding, retrieval time, freshness,
completeness, limitations, counts, truncation, and provider metadata. The
reviewer blocks unsupported evidence claims and can permit at most one bounded
supplemental retrieval.

### 2.4 Existing state and persistence

| State | Current owner | Lifetime | Durable? |
| --- | --- | --- | --- |
| `request_id` | API route | one chat request | no |
| `trace_id` | service/workflow | one chat request | no |
| LangGraph `workflow_id` | workflow | one chat request | no |
| LangGraph `thread_id` | workflow | resolved request `thread_key` | no |
| `session_id` | caller/UI plus Copilot | in-process continuity key | no |
| raw recent turns | `MemoryStore` | current process/session | no |
| working memory/episodes | in-memory repository | current process/session | no |
| routing state | `SessionRoutingStateStore`; optional compact SQLite adapter | process/session or local restart continuity | opt-in local only |
| local transcript/request status | optional SQLite `ChatRepository` | local user/conversation | opt-in local only |
| graph artifacts/Qdrant | configured data paths | operational/index lifecycle | yes, independently |

The request contract now accepts optional `conversation_id` and `request_id`,
plus bounded `X-User-ID` metadata. These values form a typed request identity
but create no trusted ownership or authorization. Gate 3's local SQLite adapters
enforce consistency between this metadata and local records; that is isolation for
development simulation, not authentication. There is no LangGraph checkpointer:
the graph is compiled without one and interrupted requests cannot resume after a
restart. The internal usage reporter has a session field, but it does not create
user or conversation ownership.

### 2.5 Current Streamlit lifecycle

The reference Streamlit UI creates `uuid4().hex` once in `st.session_state`,
keeps it for the displayed chat, sends only the current message plus optional
selected IP, and keeps display messages separately in `st.session_state`.
“Clear chat” clears displayed messages and replaces the session UUID; it leaves
the graph selection unchanged. A Streamlit rerun preserves state only for that
browser session; it is not chat persistence.

### 2.6 Product chatroom/message boundary

No Product database, Product chatroom API, or Product message persistence is in
this repository. The integration contract assigns Product the user/tenant,
chatroom ownership, displayed-message persistence, timestamps, and correlation
IDs. Copilot currently owns only transient session-scoped continuity and does
not authenticate a user or authorize a caller-supplied session ID.

## 3. Current context construction and token pressure

The final model receives the global system prompt, optional dynamic evidence
context, bounded history, and the current user message. The token estimator uses
deployment/model labels, an estimate multiplier, output reservation, and a hard
window guard. History is dropped before current evidence when needed.

### 3.1 What is included now

- Graph is scope-specific and serialized under per-scope plus global budgets.
- Knowledge chunks and citations compete in the same global dynamic budget.
- Profile and Detection retain raw payloads internally and serialize full
  minified JSON into model context when included.
- Product “views” currently select metadata/inventory/fact labels, not a
  field-level model projection. This is an implemented multi-view foundation,
  not completed view-aware context reduction.

### 3.2 Verified pressure and duplication risks

1. Full Profile and Detection JSON can consume the dynamic budget before Graph
   and Knowledge. If required Product blocks do not fit, synthesis fails safely;
   it does not silently truncate them.
2. A combined plan can retrieve Profile and Detection for each asset and then
   also add a reviewed EvidencePack summary and provider manifest. These are
   useful but overlapping representations.
3. Graph and Knowledge have bounded serializers and deduplicate some accepted
   chunks/citations, but there is no cross-request evidence cache/reuse layer.
4. Existing Product fetch caching and request-scoped Product-result reuse avoid
   some repeat calls inside a request. They are not durable freshness-aware
   evidence reuse across requests.
5. Episode summaries and recent history reduce replay, but their stores are
   process-local and raw recent turns can still add token pressure.

## 4. Root causes of the next architecture gap

| Problem | Verified root cause |
| --- | --- |
| Non-durable continuity | `MemoryStore`, episode repository, and routing-state store are dictionaries in one process. |
| No cross-conversation memory | no user/conversation identity or typed durable memory store exists. |
| Repeated retrieval | capability execution is request-scoped; only Product provider cache/request reuse exists. |
| Large synthesis input | full Product JSON is model-facing; context also includes manifest, evidence summary, graph/knowledge, history. |
| Organization knowledge rebuilt on demand | no validated organizational fact/baseline store or semantic memory index exists. |

## 5. Target identifier model

| Identifier | Owner | Meaning | Rule |
| --- | --- | --- |
| `user_id` | Product | authenticated user | never client-authoritative at Copilot boundary |
| `conversation_id` | Product | Product chatroom ID | ownership checked by Product |
| `session_id` | Copilot mapping or Product opaque value | runtime continuity key | random, tenant-scoped, not an authorization credential |
| `request_id` | Product correlation ID plus Copilot internal ID | one request trace/idempotency scope | immutable per request |

The preferred durable thread identity is `conversation_id`, which is the
Product chatroom ID. Legacy requests without it continue to use `session_id` as
their runtime thread key. `user_id` is supplied by the authenticated Product
frontend context; the local simulation may use `X-User-ID`, but that header is
development-only and is not production authorization.

## 6. Target ownership boundaries

| Boundary | Owns | Must not own |
| --- | --- | --- |
| Product users | identity, tenant, roles, authorized assets | Copilot provider credentials |
| Product chatrooms/messages | display transcript, ownership, retention, deletion | canonical operational evidence |
| Copilot thread state | compact route/active entities/recent evidence references | full raw transcript as model context |
| Copilot long-term memory | validated typed records with provenance | unvalidated assistant prose |
| Organization intelligence | approved organization facts/baselines | live Product or Graph truth |
| Semantic indexes | retrieval pointers/embeddings | canonical fact ownership |

## 7. Backward-compatible identity contract

The current `ChatRequest` remains valid and now accepts this optional identity
context without breaking legacy browser payloads:

```json
{
  "session_id": "legacy-or-runtime-session-id",
  "conversation_id": "product-chatroom-id",
  "request_id": "unique-turn-id",
  "message": "Analyze 192.168.0.149",
  "ui_context": {
    "selected_ip": "192.168.0.149"
  }
}
```

All new identifiers are optional. `conversation_id` is the preferred future
durable thread identity; `session_id` remains the active process-local
fallback. A server-generated `request_id` preserves old-client compatibility.
`X-User-ID: local-user-id` may supply typed identity metadata, but it is not
authentication or authorization. Public response and SSE event shapes remain
unchanged.

### 7.1 Possible future integration capabilities

These are proposals, not implemented endpoints: Product-owned chatroom create,
list, rename, delete, and message-history APIs; an authenticated trusted-context
adapter for a Copilot request; optional thread recovery/status; and analyst-only
memory approval, correction, export, and deletion operations. The Product
frontend would eventually pass a Product chatroom identity through its backend,
persist final displayed messages after SSE `done`, show evidence freshness and
limitations only when a stable contract is agreed, and never call Copilot
memory stores directly.

## 8. Durable short-term memory and checkpoint design

### 8.1 Ports

```text
ChatRepository          Product chatroom/message adapter
ThreadStateStore        compact Copilot thread state
LongTermMemoryStore     canonical validated memory records
SemanticMemoryIndex     embeddings for retrieval only
ProductApi adapters     existing live evidence boundaries
```

Memory/workflow code depends on these ports, not SQLite or PostgreSQL directly.

### 8.2 Working-memory schema

```text
ThreadState
  organization_id (trusted adapter scope)
  user_id (trusted adapter scope)
  conversation_id
  session_id
  version
  active_entities [max 2]
  active_route {intent, scope, direction, depth}
  working_summary
  recent_evidence_refs [bounded]
  recent_routes [bounded]
  latest_completed_turn_ref
  updated_at
```

Do not store raw provider payloads in thread state. Store capability/evidence
references with freshness and scope metadata. Use optimistic versioning or a
short transactional update for concurrent turns.

### 8.3 Thread/checkpoint lifecycle

1. Resolve the thread as `conversation_id` when supplied, otherwise `session_id`.
2. Load `ThreadState` before entity resolution.
3. Run the existing bounded workflow with a trusted request context.
4. Persist only terminal, validated state after synthesis/clarification rules.
5. Store one LangGraph checkpoint per bounded request stage only after a
   compatible SQLite checkpointer adapter is introduced.
6. Resume only a named interrupted request with matching tenant/thread and
   idempotency rules; never resume arbitrary user work.
7. Delete checkpoints with their thread retention policy.

The implemented local simulation uses SQLite for owner-scoped chat records and
compact `ThreadStateStore` continuity only. It does not store canonical long-term
memory or LangGraph checkpoints. Production should use the existing Product
PostgreSQL server through a restricted Copilot schema or Product API adapter;
no extra production database server is required.

## 9. Typed long-term memory design

Canonical records should be structured and evidence-backed:

```text
MemoryRecord
  memory_id, organization_id, subject_type, subject_id
  fact_type, value_json, epistemic_status
  confidence, source_kind, source_reference
  evidence_refs, observed_at, retrieved_at, valid_from, valid_until
  freshness_policy, supersedes_id, status
  created_by, approved_by, created_at, updated_at
  retention_class, delete_after
```

`epistemic_status` distinguishes observed, derived, analyst-approved,
interpretation, and hypothesis. Only observed/derived validated facts and
explicit analyst-approved records may become reusable organizational memory.
Hypotheses, raw chat, raw assistant prose, prompt-injected text, and unsupported
model conclusions remain non-canonical.

Fresh live Product/Graph evidence outranks memory. Memory may suggest a query,
provide a baseline, or avoid repeating clearly fresh validated work; it may not
state that a current relationship, detection, risk, or asset identity still
holds without current evidence.

## 10. Cross-conversation semantic retrieval

The repository already exposes a local BGE embedding boundary and Qdrant vector
store abstraction for documentation RAG. Reuse that boundary behind a separate
`SemanticMemoryIndex`, collection, filters, and lifecycle.

Rules:

- embed approved summaries or typed-memory retrieval text, not raw transcripts;
- filter by organization before scoring, then by memory type, subject, and
  retention/validity;
- return memory IDs and provenance, not a vector-store object as truth;
- hydrate canonical records from `LongTermMemoryStore` before context use;
- label retrieved memory as historical/derived and retain freshness limits;
- never silently fall back from unavailable semantic retrieval to unrelated
  tenant data.

General Knowledge RAG remains separate: it retrieves approved documentation,
not organization facts or thread memory.

## 11. Context-compaction design

The future composer should construct only route-relevant evidence:

```text
current request
-> compact working state
-> bounded recent turns for the matching thread/episode
-> relevant validated memory references
-> fresh live operational evidence required by TaskSpec
-> Knowledge RAG only when required
```

Required improvements:

- bounded recent history plus deterministic working summaries;
- field-level Profile/Detection projections selected by approved view and task;
- explicit full-payload escape hatch only for approved deep/evidence requests;
- one normalized fact/evidence identity across manifest, summary, and provider
  blocks to prevent duplicate context;
- delta context: include only evidence changes since the retained reference;
- route-specific allocation, with required Graph or Product context protected;
- per-source input tokens, deduped facts, omitted facts, reused evidence, cache
  age, and final-message token metrics.

The escape hatch must remain bounded, auditable, and explicit in limitations.

## 12. Memory-first workflow proposal

```text
trusted identity -> load thread state -> resolve entities
-> retrieve matching validated memory references
-> semantic route -> validate task
-> decide required fresh live capabilities
-> execute only allowed missing/current evidence
-> review evidence -> compose compact context -> synthesize
-> update thread state -> propose (not auto-promote) durable memory
```

“Memory first” means inspect compact state before repeating work. It does not
mean skip a capability mandated by TaskSpec freshness, evidence reviewer, or
authorization policy.

## 13. Local simulation architecture

Local development can add a separately enabled simulation adapter:

```text
local test user/login simulation
-> local chatroom/message SQLite tables
-> SQLite ThreadStateStore + canonical memory
-> SQLite LangGraph checkpoint adapter
-> existing local Qdrant for semantic retrieval
```

Every table and index query must filter by simulated organization/user and
conversation. Restart recovery is an acceptance criterion: completed turns,
thread state, and safe resumable checkpoints survive a local process restart.
This is a future development mode, not a change to the current Streamlit UI.

## 14. Production architecture

The current browser -> Copilot -> browser streaming shape remains valid; a BFF
is not a technical requirement for the model workflow itself. Product remains
the authority for authentication, authorization, users, chatrooms, and messages.
The preferred deployment is a Product backend adapter or restricted Copilot
schema on the existing Product PostgreSQL server. It replaces local adapters
without changing workflow ports. Copilot still reaches Product APIs for live
operational truth and keeps service credentials private.

## 15. Observability, security, and retention

Required metrics:

- request/thread/conversation correlation without secrets;
- state-load/save/checkpoint outcomes and version conflicts;
- memory candidates, approvals, rejections, supersession, and deletion;
- live retrieval skipped/reused only with freshness reason;
- per-provider and final-context tokens, duplication, compaction, and limits;
- organization/user isolation denials and audit metadata.

Security requirements:

- all stores enforce organization, user, and conversation ownership;
- Product service authentication is not replaced by a session ID;
- memory writes sanitize prompt-injection content and require evidence/approval;
- raw transcript retention follows Product policy, not default Copilot memory;
- deletion propagates to thread state, checkpoints, canonical memory, and
  semantic-index entries by stable record ID;
- retention classes define expiry, revalidation, legal hold, and tombstone rules.

## 16. Phased implementation and rollback

| Phase | Change boundary | Commit boundary | Rollback |
| --- | --- | --- | --- |
| 1 | identifier contracts and storage ports | contracts/adapter commit | use legacy session-only path |
| 2 | local SQLite Product/thread simulation | local persistence commit | disable local adapters |
| 3 | local Streamlit identity/chatroom workflow | UI simulation commit | disable simulation |
| 4 | view-aware projection, deduplication, and delta context | composer commit | retain current bounded full-payload path |
| 5 | compatible checkpointer and recovery | workflow persistence commit | compile without checkpointer |
| 6 | typed durable memory + approval flow | canonical memory commit | disable retrieval/writes |
| 7 | semantic memory index | index adapter commit | disable index, retain canonical records |
| 8 | Product persistence adapters | Product integration commit | use local/legacy adapters |
| 9 | Organization Intelligence Plane | intelligence-plane commit | disable intelligence retrieval |

Migrations require versioned schema, backfill only from validated Product or
approved records, dry-run counts, tenant-scoped rollback, and no destructive
rewrite of Product messages. A vector index is rebuildable from canonical memory
and must never be the only copy of a fact.

## 17. Test matrix and acceptance criteria

- migration and restart recovery for one thread;
- strict organization/user/conversation isolation;
- stale memory never overrides newer Product/Graph evidence;
- deleted memory cannot be returned by canonical or semantic retrieval;
- topic detachment still excludes unrelated raw history;
- explicit/UI/session entity precedence remains unchanged;
- no cross-thread context leakage under concurrent requests;
- checkpoint resume obeys request identity and executes no duplicate side effect;
- context budgets record full, projected, deduped, and omitted tokens;
- unavailable SQLite/Qdrant/Product adapters fail safely without breaking
  unrelated general requests;
- public `/chat` and SSE contracts remain backward compatible.

## 18. Likely later repository changes

| Later phase | Likely files/directories |
| --- | --- |
| ports/contracts | `app/src/core/memory/`, `app/src/core/agent/contracts.py` |
| adapters | `app/src/core/memory/adapters/`, Product integration boundary |
| workflow | `app/src/core/agent/workflow.py`, `nodes.py`, service runtime wiring |
| context | `app/src/core/context/composer.py`, `product_views.py`, evidence helpers |
| semantic memory | `app/src/core/rag/` boundary or dedicated memory-index package |
| local simulation | a separately scoped local adapter/UI only after API decision |
| Product integration | Product backend repository, not this Copilot API by default |

## 19. Open questions for Product teammates

1. Which Product identifier is the stable chatroom/conversation key?
2. Which trusted identity claims can the Product backend pass to Copilot?
3. Who owns transcript retention, export, and deletion requests?
4. Is a restricted Copilot PostgreSQL schema permitted, or must all persistence
   use Product APIs?
5. Which analyst actions approve a finding for organizational reuse?
6. What freshness TTL/revalidation policy applies to profiles, detections,
   topology, and organizational baselines?
7. Are multiple Copilot replicas planned before durable thread state is added?
8. Which response evidence/trace fields, if any, need a future frontend contract?

## 20. Implemented migration gates

Gate 2 added optional `conversation_id` and `request_id`, bounded
`X-User-ID` metadata, a resolved request/thread identity contract, and the four
storage ports. The legacy `session_id` path remains active.

Gate 3 adds disabled-by-default stdlib SQLite adapters for local chat simulation
and compact thread continuity. The schema contains local users, conversations,
messages, request commits, thread states, and schema metadata. It uses foreign
keys, parameterized SQL, explicit transactions, bounded values, typed JSON,
conversation-scoped request idempotency, owner checks, and optimistic thread-state
revisions. The official LangGraph SQLite checkpointer is deliberately deferred:
the current workflow state needs a checkpoint-safe projection before it can be
serialized without unrestricted messages or full evidence objects.

Gate 4 is now implemented as a disabled-by-default local simulation. It uses
opaque local user/conversation IDs, restores SQLite-backed transcript metadata and
approved compact continuity, preserves current API identity precedence, and does
not present local ownership metadata as Product authentication. A version-2 local
SQLite migration adds one stable `session_id` per conversation while preserving
Gate 3 records. Product adapters, full context compaction, checkpointing, typed
long-term memory, semantic retrieval, and Organization Intelligence remain later
independent gates.

Gate 5 unifies legacy and local-simulation chat behind one UI-side backend
contract and shared controller/SSE renderer. It adds one versioned bounded
`ThreadMemoryState`, an idempotent SQLite v2-to-v3 migration, persisted working
summary provenance, bounded completed-turn references, bounded episode summaries,
deterministic same-conversation relevant-turn selection, and a budgeted
`MemoryContextPackage` before final model context composition.

Still deferred are Product Memory API/PostgreSQL adapters, semantic or
cross-conversation retrieval, BGE/Qdrant memory indexing, full LangGraph
checkpointing, Organization Intelligence, and Profile/Detection multi-view or
delta context compaction. A future Product adapter must preserve owner scope,
stable session identity, optimistic revision, schema version, ordered completed
transcript reads, bounded episode retention, and non-fatal unavailable/conflict
classification; endpoint paths remain a Product contract decision.

## Appendix A. Disposition of the removed OpenCode guide

`PROJECT_ANALYSIS_AND_OPENCODE_GUIDE.md` was removed during this audit. Its
OpenCode platform material was tool-specific and outside the Copilot product
documentation. Its Soorin architecture inventory, workflow description, RAG,
memory, deployment, and test summaries duplicated verified current material in
`CURRENT_ARCHITECTURE.md`, the frontend/backend integration contract, and this
future-design document. Its remaining issue list was historical and contained
unverified or superseded findings, so it was not retained as current guidance.
