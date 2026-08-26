# Soorin Copilot Frontend and Backend Integration Contract

**Production integration audit:** 2026-08-17
**Method:** static source, local Git history, schemas, and offline-test collection only. No services, Product APIs, models, Qdrant, Docker, or network calls were started. Code is authoritative.

## Status legend

| Label | Meaning |
| --- | --- |
| **Implemented** | Present in the named code path. |
| **Local only** | Intended solely for the opt-in SQLite/Streamlit simulation. |
| **Proposed** | Required design for Product production integration; not an implemented endpoint or schema. |

## 1. Git baseline and the three states

| State | Revision | What it represents |
| --- | --- | --- |
| **CURRENT DEPLOYED MAIN / TAG** | `main` / `origin/main`: `1a02d522` (`prompts: tune router and planner for DeepSeek`); latest local release tag: annotated `copilot_release_3.2.1` at `eff75ed94` (`refactor(deploy): unify portable production deployment`) | The tag is an ancestor of `main`; repository history does not identify the exact commit deployed to Product after that tag. Treat the tag as the last identifiable release and `main` as the latest untagged mainline source. |
| **CURRENT DEV IMPLEMENTATION** | `dev` / local `origin/dev`: `4a61b840` (`refactor: router-planner dynamic system prompts tuning`) | Adds RequestIdentity, local persistence adapters, compact thread state, typed LTM lifecycle/retrieval, bounded memory workflow, and the prompt architecture. |
| **TARGET PRODUCTION MEMORY INTEGRATION** | Proposed | Product remains canonical for users, tenants, chatrooms, and messages. Product PostgreSQL gains Copilot memory records or exposes an equivalent trusted memory service. |

`main...dev` contains the request-identity and memory integration sequence beginning at `4081398` (`Refactor(api): add conversation and request identity contracts`), local simulation/persistence, typed LTM, semantic retrieval, lifecycle stabilization, and prompt changes. It is not deployed merely because it exists on `dev`.

## 2. CURRENT DEPLOYED MAIN / TAG

### 2.1 Request and persistence contract

The `main` source has a local `ChatRequest` with only `session_id`, `message`, and optional `ui_context`. `/chat` and `/chat/stream` create a server request ID (`uuid4().hex[:12]`) and pass it internally; clients do not supply `conversation_id` or `request_id`.

```json
{
  "session_id": "<browser-generated/reused UUID>",
  "message": "…",
  "ui_context": {"selected_ip": "203.0.113.8"}
}
```

`app/app_st.py` on `main` creates/replaces its session UUID in browser state and sends exactly the body above (omitting `ui_context` when no selected IP exists). Therefore the documented deployed Product flow is compatible with a Product chatroom ID only by treating it as the legacy `session_id`; it does **not** send separate conversation/request IDs.

```text
Product browser                         Copilot main/tag
  Product login/chatroom selection       static API-key check
  ── POST /chat/stream ───────────────►  session continuity in process
  ◄──────────── UTF-8 SSE ─────────────  answer
  ── Product chatroom/message APIs ───► Product backend ─► Product PostgreSQL
```

Product creates/owns chatrooms and stores user/assistant messages in its PostgreSQL database. Copilot does not create Product chatrooms or persist Product transcripts. The deployed Copilot session is an application continuity key, not a Product ownership credential.

### 2.2 Authentication fact, not assumption

Both `main` and current `dev` authenticate Copilot with the configured static `SOORIN_COPILOT_API_KEY`. The static key can be sent as `Authorization: Bearer <copilot key>`. Current `dev` additionally accepts it in `Soorin_copilot_api_key`, allowing a Product JWT to remain in `Authorization`.

The Copilot repository does **not** decode, validate, or authorize a Product JWT. A Product JWT in `Authorization` is therefore not a current Copilot identity contract unless the custom static-key header is also supplied. Product authentication/ownership checks remain Product Backend duties. The historical direct-browser key exposure is a known limitation.

## 3. CURRENT DEV IMPLEMENTATION

### 3.1 Exact `/chat` and `/chat/stream` request identity

`ChatRequest` now accepts these nullable fields, all normalized to a conservative identifier (`1..128` characters; `[A-Za-z0-9][A-Za-z0-9._:@-]*`):

```json
{
  "conversation_id": "<optional>",
  "session_id": "<optional>",
  "request_id": "<optional>",
  "message": "<required, non-empty>",
  "ui_context": {"selected_ip": "<optional IPv4>"}
}
```

`RequestIdentity.resolve` preserves supplied IDs; absent `session_id` becomes `uuid4().hex`, absent `request_id` becomes `uuid4().hex[:12]`, and:

```text
thread_key = conversation_id when supplied, otherwise session_id
```

`conversation_id` is identity/continuity metadata only today. It is not checked against a Product chatroom. `X-User-ID` is an optional, syntactically validated header passed into `RequestIdentity.user_id`; it is **untrusted metadata**, not authentication or authorization. It must never be treated as trusted Product identity in production.

Current protected requests use either:

```http
Authorization: Bearer <SOORIN_COPILOT_API_KEY>
```

or, when the browser retains Product JWT:

```http
Authorization: Bearer <PRODUCT_JWT>
Soorin_copilot_api_key: <SOORIN_COPILOT_API_KEY>
```

The latter authenticates only the static Copilot key. The custom header takes precedence if both mechanisms are present. `X-User-ID` alone is rejected as unauthorized.

### 3.2 Local Streamlit Product simulation — Local only

The local-simulation client creates a local user/conversation, retains the stable local `conversation_id` and `session_id`, creates a browser-side `request_id`, and streams:

```http
POST /chat/stream
Authorization: Bearer <local Copilot API key>
Soorin_copilot_api_key: <local Copilot API key>
X-User-ID: <local simulation user ID>
Content-Type: application/json
```

```json
{
  "conversation_id": "local-chat-<uuid>",
  "session_id": "<stable local session>",
  "request_id": "<uuid4 hex>",
  "message": "…",
  "ui_context": {"selected_ip": "203.0.113.8"}
}
```

It reloads local conversations/messages, supports local create/open/delete, and persists completed turns only through the local repository. It approximates the required ID lifecycle and SSE behavior, but does **not** simulate Product JWT validation, tenant/workspace isolation, Product ownership checks, or Product PostgreSQL. The legacy Streamlit backend remains compatible with only `session_id`, `message`, and optional UI context.

### 3.2.1 Product-backed local Streamlit workspace — Implemented on `dev`

`SOORIN_STREAMLIT_AUTH_BACKEND=product` dispatches before local graph/SQLite initialization. The browser session performs real Product login and Product-owned chatroom/message CRUD through `SOORIN_PRODUCT_CHAT_ROOMS_PATH`. The selected Product room ID is reused as both `conversation_id` and `session_id`; the authenticated Product user ID is sent as `X-User-ID`. `/chat/stream` receives the interactive JWT in `Authorization`, the static Copilot key in `Soorin_copilot_api_key`, and a stable per-turn `request_id`.

The Product workspace reuses the canonical shared chat renderer and SSE parser. It appends the user message before starting the stream, requires a terminal `done` event before appending the assistant message, and guards both writes against duplicate reruns. A selected topology IP is preserved in `ui_context`; new Product rooms use that IP as `assetIp`, or an explicit empty value when no asset is selected. Product mode has no local transcript or SQLite fallback; a `401` clears the Product browser session and returns to login.

### 3.3 Current SQLite schema (version 7; thread-state payload version 3)

All SQLite persistence is opt-in development functionality. `PRAGMA foreign_keys=ON` is used. Product-owned simulation tables must **not** be copied as new production tables because Product already owns their equivalents.

| Table | Exact columns / constraints | Ownership and role |
| --- | --- | --- |
| `schema_metadata` | `key TEXT PRIMARY KEY`, `value TEXT NOT NULL` | schema version bookkeeping. |
| `local_users` | `user_id TEXT PRIMARY KEY`, `username TEXT`, `password_hash TEXT`, `created_at TEXT NOT NULL`; unique case-insensitive username when non-null | **Local only** user simulation. |
| `local_conversations` | `conversation_id TEXT PRIMARY KEY`, `user_id TEXT NOT NULL REFERENCES local_users ON DELETE CASCADE`, `session_id TEXT NOT NULL UNIQUE`, `title TEXT NOT NULL DEFAULT ''`, `created_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`; index `(user_id, updated_at DESC, conversation_id)` | **Local only** Product-chatroom surrogate. |
| `local_messages` | `message_id TEXT PRIMARY KEY`, `conversation_id TEXT NOT NULL REFERENCES local_conversations ON DELETE CASCADE`, `request_id TEXT NOT NULL`, `role TEXT NOT NULL CHECK (role IN ('user','assistant'))`, `content TEXT NOT NULL`, `status TEXT NOT NULL CHECK (status IN ('completed','interrupted','failed'))`, `position INTEGER NOT NULL CHECK (position > 0)`, `created_at TEXT NOT NULL`; unique `(conversation_id, request_id, role)` and `(conversation_id, position)`; index `(conversation_id, position)` | **Local only** transcript surrogate. |
| `local_request_commits` | `user_id TEXT NOT NULL REFERENCES local_users ON DELETE CASCADE`, `conversation_id TEXT NOT NULL REFERENCES local_conversations ON DELETE CASCADE`, `request_id TEXT NOT NULL`, `status TEXT NOT NULL CHECK (status IN ('started','interrupted','completed','failed'))`, timestamps; primary key `(user_id, conversation_id, request_id)` | **Local only** idempotent turn commit state. |
| `local_thread_states` | `thread_key TEXT PRIMARY KEY`, nullable `user_id`, nullable `conversation_id`, `session_id TEXT NOT NULL`, `revision INTEGER NOT NULL CHECK (revision > 0)`, `schema_version INTEGER NOT NULL`, `state_json TEXT NOT NULL`, `updated_at TEXT NOT NULL` | Copilot compact continuity. JSON is bounded to 16,384 bytes and saved with optimistic revision. Production needs trusted owner/conversation enforcement absent from this local row. |
| `local_long_term_memories` | `memory_id TEXT PRIMARY KEY`, `user_id TEXT NOT NULL REFERENCES local_users ON DELETE CASCADE`, `memory_type`, `statement`, `epistemic_status` non-null, `confidence REAL CHECK (0<=confidence<=1)`, source request/conversation IDs, `evidence_refs_json TEXT NOT NULL`, provenance, validity/timestamps, `revision INTEGER CHECK (>0)`, lifecycle `status`, `index_status`, `supersedes_memory_id`, fingerprint/logical-key/conflict/policy fields | Copilot canonical typed LTM, locally scoped to a simulation user. See §3.5. |
| `local_long_term_memory_entities` | `memory_id TEXT NOT NULL REFERENCES local_long_term_memories ON DELETE CASCADE`, `entity_id TEXT NOT NULL`, primary key `(memory_id, entity_id)`; index `(entity_id, memory_id)` | LTM entity link. |
| `local_long_term_memory_audit` | `event_id TEXT PRIMARY KEY`, `memory_id`, logical key/fingerprint, `user_id`, `entity_ids_json`, source request ID, action/from/to/reason/policy/evidence/actor/timestamp fields all non-null | append-only lifecycle history; deliberately no FK to allow retained history after candidate eviction. Indexes `(user_id, created_at DESC, event_id)` and `(memory_id, created_at DESC, event_id)`. |

LTM indexes additionally include owner/status/time, owner/type/epistemic/status, partial unique live fingerprint (`candidate`/`active`), partial unique active logical key, and logical-key lookup. Local policy bounds a user to 100 conversations, 200 messages per conversation, 500 active LTM, and 250 candidate LTM. Candidate quota can evict candidates; active quota rejects new active promotion.

### 3.4 Thread, working, and episodic state

`ThreadMemoryState` (also called compact thread state) persists identity (`thread_key`, nullable user/conversation, session), active/last-resolved entities, prior entity mode/count, prior route dimensions, provider metadata, prior detection/profile requirements, optional `WorkingMemory`, bounded turn references, bounded episodes, summary metadata, `updated_at`, `revision`, and schema version.

`WorkingMemory` is session-scoped and contains `session_id`, `MemoryContextKey`, `episode_id`, latest user/assistant turns, compact summary, providers/scope, limitations, and typed facts. `WorkingFact` has `key`, `value`, `fact_type` (default `user_provided`), scope (`conversation` or `entity`), entity IDs, and creation timestamp; entity scope requires an entity binding. `EpisodeRecord` contains its ID, session/context key, timestamps, compact summary, supported findings, facts, inferred role, findings, contradictions, unresolved questions, next checks, evidence scope, limitations, providers, scope, and turn count.

`MemoryContextKey` uses entities, topic family, relationship mode, and scope family; it intentionally does not use the literal user message. Serialization validates strict item/text bounds (for example 20 facts/episodes/turn references in persistence representations; 4,000-character working summary) and rejects malformed/future/corrupt state.

### 3.5 Typed long-term memory and lifecycle

`LongTermMemoryRecord` fields are exactly: `memory_id`, `memory_type`, `user_id`, `entity_ids`, `statement`, `epistemic_status`, `confidence`, `source_request_id`, `source_conversation_id`, `evidence_refs`, `provenance_category`, `valid_from`, `valid_until`, `created_at`, `updated_at`, `revision`, `status`, `index_status`, `supersedes_memory_id`, `idempotency_fingerprint`, `logical_memory_key`, `has_unresolved_conflict`, and `policy_version`.

There is no separate `observed_at`, `validated_at`, or `superseded_by` field in the current contract. Observation time is normalized into `valid_from`; `valid_until` expresses freshness. Supported types are validated finding, investigation outcome, analyst correction, approved asset fact, known benign behavior, and hypothesis resolution. Epistemic values are candidate, analyst-confirmed, source-validated, historical, or unconfirmed. Lifecycle states are candidate, active, invalidated, superseded, rejected, or expired.

The canonical store operations are `get`, `put`, revision-checked `update`, owner/entity/type/status filtered `list`, `apply_promotion`, audit listing, `invalidate`, `expire`, revision-checked `supersede`, and `delete`. `apply_promotion`, confirmation/deduplication, changed-value supersession/conflict decision, lifecycle-audit insertion, and quota decisions must be transactional. Optimistic revision checks prevent lost updates. A structured safe Product result becomes a candidate, deterministic policy evaluates it, and a decision may promote, retain/review, reject, confirm a same-value active record, supersede a changed compatible active record, or retain a conflict candidate. Candidate and active identity are owner-scoped through both fingerprint and logical key.

### 3.6 BGE/Qdrant is a discovery index, never authority

Long-term memory and vector indexing are disabled by default (`SOORIN_LONG_TERM_MEMORY_ENABLED=false`, `SOORIN_MEMORY_VECTOR_INDEX_ENABLED=false`). When enabled, a separate Qdrant collection (default `soorin_copilot_memory_v1`) uses the existing BGE embedding setup; the configured default embedding is `BAAI/bge-base-en-v1.5`, dimension 768. It must not share the SOC knowledge collection.

The index stores memory ID plus owner, entities, lifecycle, epistemic/freshness, revision, source, and retrieval projection metadata. Search filters `user_id` and `status=active`, then every hit is reloaded from the canonical store and fails closed on owner, lifecycle, epistemic authority, conflict, freshness, entity, score, or token-budget checks. Qdrant failure adds a limitation and falls back to canonical exact retrieval; canonical writes remain durable and record index failure status. Vector search is therefore retrieval/discovery only, never canonical memory authority.

### 3.7 Current memory read/write path

```text
request → RequestIdentity → thread-state load(thread_key, identity)
→ restore routing/working/episodes; local transcript restore only when local repository + user + conversation
→ entity resolution → eligible active LTM exact retrieval (+ optional semantic discovery)
→ Router → TaskSpec → EvidenceRequirements/Gate 8
→ direct plan or bounded Planner → PlanValidator → only missing capabilities
→ ToolResults → EvidencePack → retrieval review → at most one supplement
→ bounded MemoryContextPackage/ContextComposer/token guard → Synthesizer
→ update working facts/episode/raw in-process state → build LTM candidate/promotion
→ persist compact thread state with expected revision; locally commit or finalize local request.
```

Reads are owner-scoped only when a `user_id` is present. The service restores durable state before workflow execution and saves terminal compact state after it. Local transcript/request-commit writes are deliberately guarded by the local user+conversation pair. LTM promotion writes are canonical and transactional in the local adapter; semantic index synchronization is best effort after canonical lifecycle application.

## 4. TARGET PRODUCTION MEMORY INTEGRATION — Proposed

> **2026-08-23 Product LTM lifecycle integration:** `ProductMemoryClient`,
> `ProductThreadStateStore`, and `ProductLongTermMemoryStore` are implemented.
> Set `SOORIN_LONG_TERM_MEMORY_BACKEND=product` to select the Product/PostgreSQL
> canonical LTM authority. The adapter canonically looks up an active record by
> logical key, then sends one configured `/transition` mutation: `promote` when
> none exists, `confirm` for an equal value, `supersede` for a compatible change,
> and `conflict` for a material contradiction. The Backend atomically performs related record changes,
> optimistic revision enforcement, and audit writes. SQLite remains available as
> the local/reference backend. Qdrant is semantic discovery only and is never a
> canonical authority or a fallback write target.

### 4.1 Recommended browser contract and identity model

After Product migration, require a Product chatroom ID as `conversation_id`, equal to the canonical Product chatroom ID. Retain `session_id` only as legacy compatibility during migration; do not make it a second ownership source. Use Product-generated or Product-backend-generated immutable turn/request IDs where possible (browser-generated UUID is acceptable only if Product/BFF binds it idempotently). Copilot may still generate a fallback for legacy callers, but a memory-enabled production request should require a stable trusted request ID.

```http
Authorization: Bearer <PRODUCT_JWT>
Soorin_copilot_api_key: <COPILOT_SERVICE_KEY>
```

```json
{
  "conversation_id": "<Product chatroom ID>",
  "session_id": "<legacy migration key only>",
  "request_id": "<Product-bound idempotency key>",
  "message": "…",
  "ui_context": {"selected_ip": "203.0.113.8"}
}
```

Trusted memory isolation ultimately needs at least `tenant_id`/`organization_id`, `user_id`, `workspace_id` when Product scopes data that way, and `conversation_id`. The repository currently carries only optional `user_id` and `conversation_id`; it has no tenant/workspace fields and does not validate Product ownership. A plain browser-supplied user ID, including `X-User-ID`, must remain metadata and must not authorize memory.

Product JWT plus `conversation_id` can resolve canonical ownership only when a trusted Product backend validates the JWT and checks chatroom membership. Rank the integration choices:

1. **B — Product Backend/BFF issues short-lived signed Copilot context:** strongest separation and no static Copilot key in the browser; moderate Product work.
2. **A — Direct browser to Copilot; Copilot validates/resolves the Product JWT through Product Backend:** secure when token validation, audience, issuer, expiry, tenant, and conversation ownership are enforced; higher Copilot/Product coupling and latency.
3. **C — Browser supplies `user_id`:** lowest migration cost and unacceptable as authorization. It is usable only as non-authoritative display metadata with Product verification.

### 4.2 Proposed minimum Product Memory API responsibilities

These are operations, not endorsed URLs or final payloads.

| Operation | Caller and authorization | Required request/response semantics |
| --- | --- | --- |
| Resolve trusted request context | Copilot/BFF; validate Product identity and chatroom membership | Input token/signed context plus conversation/request ID. Output immutable tenant, user, workspace (if applicable), canonical conversation, and authorization decision. Must reject cross-tenant/chatroom access. |
| Load/save compact thread state | Copilot; trusted context required | Key by tenant/user/conversation/thread key. Load typed JSON/version/revision; save with expected revision and conflict response; delete on authorized conversation deletion/retention. |
| Put/evaluate/promote LTM candidate | Copilot/system or approved analyst; trusted owner context | Typed record, fingerprint, logical key, evidence provenance, policy version, idempotency key. Return canonical record, lifecycle decision, revision, audit event(s), conflict/supersession result. Must be one transaction. |
| Get/list exact active LTM | Copilot; trusted owner context | Owner/entity/type/status/epistemic filters, bounded pagination/limits. Return canonical records only, not vector payload authority. |
| Lifecycle operations and audit | Copilot/authorized analyst; role-aware | Revision-checked invalidate, expire, supersede, delete and bounded audit list. Each lifecycle mutation is transactional and appends immutable audit history. |
| Semantic-index outbox/status | Copilot worker/service | Publish canonical post-commit changes; return/persist index state. Index failures must not roll back canonical records. |

### 4.3 Proposed Product PostgreSQL additions

Do not duplicate Product `users`, chatrooms/conversations, or messages. Add Copilot-owned records associated with those existing concepts (actual FK table/column names remain a Product decision):

| Proposed table | Essential columns and constraints |
| --- | --- |
| `copilot_thread_state` | `thread_key` PK (or tenant/conversation composite key), tenant/user/workspace/conversation FKs or immutable IDs, legacy `session_id`, `state_json JSONB`, `schema_version`, `revision`, timestamps. Unique owner+conversation/thread binding; index owner/conversation and update time. Enforce trusted membership before read/write. |
| `copilot_long_term_memory` | UUID/text memory PK; tenant/user/workspace and source conversation IDs; type, statement/payload JSONB or text, epistemic/provenance/status/index-status enums/checks, confidence numeric check, evidence refs JSONB, validity/timestamps, revision, supersedes ID, fingerprint, logical key, conflict flag, policy version. Partial unique `(tenant,user,idempotency_fingerprint)` for live candidate/active; partial unique `(tenant,user,logical_memory_key)` for active; owner/status/time and owner/type/epistemic indexes. |
| `copilot_memory_entity_link` | memory FK, normalized entity ID, composite PK; entity lookup index. |
| `copilot_memory_audit` | immutable event PK, memory ID (may intentionally omit FK to retain evicted-candidate history), trusted owner fields, entity IDs JSONB, source request ID, action/from/to/reason/policy/evidence/actor/timestamp; owner/time and memory/time indexes. |
| `copilot_memory_index_outbox` (recommended) | canonical memory/version/action, retry state, error metadata, timestamps, idempotency uniqueness. Lets Qdrant synchronize asynchronously after a committed canonical transaction. |

Retention/deletion must follow Product tenant/user/chatroom policy. Conversation deletion must not automatically delete user-scoped LTM without an explicit Product retention rule; thread state may be deleted with the conversation. Tenant deletion must cascade or be cryptographically/operationally erased across canonical and vector projections. The precise choice of tenant/workspace foreign keys, RLS policy, enum versus check constraints, and transcript-retention relationship is a Product schema decision, not determined by this repository.

## 5. Implemented versus proposed summary

| Capability | Main/tag | Dev | Target production |
| --- | --- | --- | --- |
| Product chatroom/messages in Product PostgreSQL | documented Product responsibility | unchanged | remains Product-owned |
| `conversation_id`/`request_id` request fields | absent | optional | required for memory-enabled Product flow |
| Product JWT validation by Copilot | absent | absent | BFF signed context or Copilot→Product validation |
| `X-User-ID` trusted identity | absent | no; metadata only | never direct authorization |
| Thread/working/episodic durability | in-process only | SQLite local adapter | Product PostgreSQL/service adapter |
| Typed LTM/lifecycle/audit | absent | SQLite local adapter | Product PostgreSQL/service adapter |
| BGE/Qdrant LTM discovery | absent | optional, disabled | post-commit projection only |

## 6. Verification performed

- Read-only Git inspection: `status`, branches, tag ancestry, `log`, `show`, and `diff main...dev`.
- Offline test collection: 92 relevant tests across request identity, authentication/CORS, local simulation, SQLite persistence, and LTM lifecycle were discovered.
- Four focused offline assertions passed: legacy request/session fallback, Product-JWT-plus-custom-Copilot-key authentication, thread-state owner/conversation/revision isolation, and atomic changed-value LTM supersession.
- `python -m compileall -q app/src` completed successfully.
- No runtime integration assertion is claimed: the combined pytest execution did not provide a complete terminal summary in this environment, so it is not reported as a passing suite.

This document supersedes historical integration claims where they conflict with the source at the revisions above.

> **Known deployment blocker (2026-08-26):** offline adapter contract tests cover canonical create/search/get and atomic lifecycle request mapping, but the latest real Product deployment returned HTTP 500 for a valid `promote` transition after candidate creation and an empty active lookup. This remains a Product Backend/deployment blocker; Copilot does not change routes, invent alternate payloads, or fall back to SQLite. Product chat-room messages are now read-only canonical transcript input for ThreadState turn-reference reconstruction; Streamlit remains the sole Product transcript writer.
