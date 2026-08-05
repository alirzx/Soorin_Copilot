# Soorin Copilot Frontend and Backend Integration Contract

Audit baseline: `dev` at `f9a62c6` plus the verified Gate 3 working tree on
2026-08-05.

This document separates **current verified behavior** from **production
recommendations**. Current behavior was verified from route and service source,
Pydantic schemas, tests, configuration, the Streamlit client, and the documented
Product chatroom/message flow. No service or external provider was started.

## 1. Purpose and audience

This is the integration contract for Product frontend, Product backend,
security, QA, and Copilot maintainers. It answers four practical questions:

1. Which Copilot endpoint does the Product browser call directly?
2. What exact JSON and streaming frames exist today?
3. Which identity, authorization, session, timeout, and persistence duties do
   not belong to the Copilot runtime?
4. Which Graph and Product endpoints are public helpers versus internal data
   providers?

Postman assets accompanying this document are test/documentation tools.

## 2. Production architecture

The current Product topology is direct streaming:

```text
Product browser
  |  Product login/session and selected chatroom
  |  POST Copilot /chat/stream directly; receive SSE directly
  |  current Copilot static key supplied by browser integration
  v
Soorin Copilot API                                  (this repository)
  |  validates static Copilot API key on every request except GET /health
  |-- Arvan-compatible Router / optional Planner / Chat deployments
  |-- Product login and bearer-token manager
  |-- Product profile endpoint
  |-- Product asset-detection endpoint
  |-- Product topology endpoint used by background Graph refresh
  |-- Product LLM-usage reporting endpoint (when enabled)
  |-- local NetworkX last-known-good Graph snapshot
  |-- Qdrant knowledge collection (optional)
  `-- Hugging Face embedding model cache (optional, lazy)

Product browser
  |  after a completed turn, store user/assistant messages
  v
Product Backend chatroom/message endpoints
  v
Product PostgreSQL
```

There is no mandatory Product backend proxy or BFF in the current chat path.
A gateway remains an optional future security-hardening choice, but the present
browser calls Copilot directly and separately uses Product Backend endpoints for
chatroom/message persistence.

## 3. Trust boundaries

| Boundary | Trusted responsibility | Untrusted input |
|---|---|---|
| Browser -> Copilot | current message, session/chatroom continuity, selected IP, Copilot key | all message and UI fields remain untrusted input |
| Browser -> Product backend | Product authentication, chatroom ownership, message persistence | chatroom/message input |
| Copilot -> Product API | shared Product client, bearer token, `x-hwid`, configured paths | Product JSON is validated at provider boundaries |
| Copilot -> Arvan | provider credentials and normalized messages | model output is routed through validation/review |
| Copilot -> Graph/Qdrant | configured local or server data stores | snapshots may be stale, incomplete, or unavailable |

**Authentication:** every Copilot endpoint except `GET /health` requires a
static Copilot API key (`SOORIN_COPILOT_API_KEY`). Direct service callers may send:

```
Authorization: Bearer <SOORIN_COPILOT_API_KEY>
```

When the Product frontend keeps its Product JWT in `Authorization`, it may send
the Copilot credential separately:

```http
Soorin_copilot_api_key: <SOORIN_COPILOT_API_KEY>
```

The custom header takes precedence when present. Copilot does not decode or
validate the Product JWT; Product authentication and authorization remain the
Product Backend's responsibility for Product endpoints.

Missing or invalid Copilot credentials return `401`. The static key does not
identify or authorize a Product user, tenant, chatroom, or asset. In the current
direct-browser flow the key is exposed to browser code, which is a known security
limitation. Restrict exposure and origin/network access; an optional future
gateway can move this credential out of the browser.

## 4. Current direct-browser request flow

Recommended normal chat flow:

```text
1. Frontend creates or opens a Product chatroom through Product Backend APIs.
2. The Product chatroom ID may be sent as `conversation_id`; legacy clients
   continue to reuse their `session_id`.
3. Frontend POSTs directly to Copilot `/chat/stream`.
4. Copilot returns UTF-8 SSE directly to the Frontend.
5. Frontend incrementally appends answer deltas and completes only on `done`.
6. Frontend stores the user and completed assistant messages through existing
   Product Backend chatroom/message endpoints.
7. Product Backend stores those records in Product PostgreSQL.
8. Every later turn reuses the Product chatroom and its Copilot continuity ID.
```

Product Backend owns chatroom/message persistence and ownership checks. Copilot
does not validate Product chatroom ownership, and neither
`conversation_id` nor `session_id` is an authorization credential.

For local development only, Gate 3 can opt into a SQLite-backed Product-chat
simulation and compact thread-state restoration. It is disabled by default and
does not change this API contract, authenticate `X-User-ID`, or replace Product
Backend persistence. The relevant settings are:

```env
SOORIN_LOCAL_PRODUCT_SIMULATION_ENABLED=false
SOORIN_THREAD_STATE_BACKEND=memory
SOORIN_LOCAL_SQLITE_PATH=data/runtime/copilot-local.sqlite3
SOORIN_LANGGRAPH_CHECKPOINT_BACKEND=none
```

The checkpoint setting is reserved: selecting `sqlite` currently logs a safe
deferral because a checkpoint-safe workflow-state projection is not implemented.

## 5. Internal Copilot-to-Product request flow

These calls are made by Copilot through the shared `ProductApiClient`. They are
not browser contracts.

| Call | Configured default path | Purpose | Auth and headers | Timeout/retry/cache |
|---|---|---|---|---|
| `POST` Product login | `/auth/login` | obtain `accessToken` | `x-hwid`, `x-captcha-bypass`, JSON username/password | connect/read defaults 60/300s; no automatic POST status retry |
| `GET` asset profile | `/profile/{ip}` | point-in-time identity/profile evidence | `Authorization: Bearer ...`, `x-hwid`, `Accept: application/json` | GET retry policy; independent in-memory cache, minimum TTL 600s; optional stale-on-error |
| `GET` asset detection | `/asset-detection/test/{ip}` | point-in-time detection evidence | same | same cache policy in a separate provider namespace |
| `GET` topology pairs | `/zeek/connections/unique-ip-pairs` | build/refresh NetworkX Graph | same | GET retry policy; persisted and validated last-known-good snapshots |
| `POST` LLM usage | configured `LLM_USAGE_REPORTING_URL` | one aggregate token report per Copilot request when enabled | same plus `Content-Type` and `Idempotency-Key` | non-fatal; no automatic POST status retry |

GET retry defaults are five retries, backoff factor 3, for 429/500/502/503/504.
Every Product request may make one additional attempt after a 401 by invalidating
and reacquiring the token. A configured bootstrap token is reused; with login
credentials it refreshes after the configured token age (default 600 seconds).
Without login credentials, an aged bootstrap token is reused until a 401.

Nested runtime flow:

```text
Browser -> Copilot /chat/stream
  -> Product profile/detection providers and/or local Graph/Qdrant
  -> Router/Planner/Chat models
  -> Copilot SSE -> Browser
  -> Product Backend chatroom/message endpoints -> Product PostgreSQL
```

## 6. Public endpoint matrix

All current success responses are UTF-8 JSON except `/chat/stream`. Copilot-layer authentication accepts either the service Bearer key (`Authorization: Bearer <SOORIN_COPILOT_API_KEY>`) or, when Product keeps its JWT in `Authorization`, `Soorin_copilot_api_key: <SOORIN_COPILOT_API_KEY>`. Every endpoint except `GET /health` requires a valid Copilot key; protected endpoints return `401` without one.

| Method and path | Purpose | Input | Success | Errors and limits |
|---|---|---|---|---|
| `GET /health` | process liveness | none | 200 `{"status":"ok"}` does not test models, Product API, Graph, or Qdrant | no Authorization header required |
| `GET /llm/health` | configured deployment readiness | none | 200 envelope with chat/router/planner metadata | configuration only; no model call; requires a valid Copilot key |
| `POST /chat` | complete non-stream answer | `ChatRequest` | 200 `ChatResponse` | validation 422; handled `LLMError` is a 200 error envelope; requires a valid Copilot key |
| `POST /chat/stream` | interactive answer | `ChatRequest` | 200 UTF-8 SSE | validation 422 before stream; runtime failures are SSE `error` after 200; requires a valid Copilot key |
| `GET /graph/status` | Graph load/refresh diagnostics | none | 200 `GraphStatusResponse` | may reveal local artifact paths; requires a valid Copilot key |
| `GET /graph/stats` | aggregate snapshot statistics | none | 200 `GraphStatsResponse` | no pagination; Graph unavailable may surface as server error; requires a valid Copilot key |
| `GET /graph/nodes/{ip}` | node existence/degree | syntactically valid IP | 200 `GraphNodeResponse` | malformed IP 422; absent node is 200 `found=false`; requires a valid Copilot key |
| `GET /graph/nodes/{ip}/neighbors` | direct observed peers | `direction`, `limit` | 200 `GraphNeighborsResponse` | `direction=in|out|both`; default 20; configured max default 1000; no cursor/offset; requires a valid Copilot key |
| `GET /graph/nodes/{ip}/context` | compact node context | valid path IP | 200 `GraphContextResponse` | fixed internal top-peer bound; no client limit; requires a valid Copilot key |
| `GET /graph/path` | directed shortest observed Graph path | required `source`, `target` | 200 `GraphPathResponse` | malformed/missing query 422; no hop-limit input; requires a valid Copilot key |

FastAPI/Pydantic query validation uses HTTP 422 with a `detail` array. Custom IP
validation uses HTTP 422 with `{"detail":"Invalid <field>."}`. Unexpected,
unhandled runtime exceptions use normal server error behavior.

## 7. Which endpoints the Product frontend should use

The browser calls `/chat/stream` on Copilot directly. Product-owned routes remain
responsible for Product chatroom/message persistence and the Product's canonical
live Graph UI.

| Copilot endpoint | Direct browser use | Recommendation |
|---|---:|---|
| `/chat/stream` | Yes | primary interactive chat transport |
| `/chat` | Optional | fallback, tests, accessibility/non-interactive flows; do not retry after a stream already emitted text |
| `/graph/stats` | Optional | small Copilot-snapshot summary only if Product accepts snapshot semantics |
| `/graph/nodes/{ip}` | Optional | Copilot-specific side panel helper |
| `/graph/nodes/{ip}/neighbors` | Optional | bounded helper, not a production paginated graph browser |
| `/graph/nodes/{ip}/context` | Optional | Copilot-specific evidence preview |
| `/graph/path` | Optional | observed communication path helper, not physical routing |

If the Product already renders its canonical live Graph, continue using the
Product Graph APIs for that UI. Pass only its currently selected asset to chat
as typed `ui_context.selected_ip`. Mixing Product live topology with Copilot's
snapshot lists can show inconsistent nodes, edges, or freshness.

## 8. Which endpoints remain backend/internal/diagnostic

| Endpoint or provider | Classification | Reason |
|---|---|---|
| `/health` | backend/monitoring | shallow process liveness |
| `/llm/health` | backend/admin diagnostic | reveals deployment readiness and model metadata |
| `/graph/status` | backend/admin diagnostic | refresh state plus local snapshot path fields |
| Product login/profile/detection/topology/usage | Copilot internal only | credentials, service policy, evidence integrity |
| Arvan model endpoints | Copilot internal only | API credentials and orchestration contract |
| Qdrant and Hugging Face cache | Copilot internal only | storage/model implementation details |

Do not expose diagnostics to ordinary users without field filtering and role
authorization. In particular, omit `raw_snapshot_path` and
`processed_snapshot_path` from a normal frontend response.

## 9. Session ID lifecycle

Current facts:

- `session_id` is optional and nullable.
- Supplied values are trimmed, limited to 128 conservative identifier
  characters, and reject blank/control-character input with HTTP 422.
- An absent value is replaced by a server-generated 32-character UUID4 hex
  string.
- The resolved ID is returned in `/chat` data and `/chat/stream` `done.data`.
- State is keyed only by this string, not by user or tenant.
- Conversation and routing state are in process memory and are lost on restart.
- Multiple API replicas do not share state. Without sticky routing, follow-ups
  can reach a replica with no prior context.

Product ownership:

1. Frontend may create `crypto.randomUUID()` for a new chat.
2. Product Backend verifies that the Product chatroom is owned by the
   authenticated user.
3. Frontend reuses the chatroom's Copilot continuity value for direct calls.
4. It rejects cross-user reuse, regardless of UUID entropy.
5. A new-chat action creates a new ID.

The request identity contract uses Product `conversation_id` as the preferred
future durable thread key and retains `session_id` as the active legacy/runtime
fallback. Neither field is an authorization decision.

## 10. UI context and selected-IP lifecycle

Current request shape:

```json
{
  "session_id": "8c3173b4-b153-4fcb-91b8-7fb86b063f97",
  "message": "What is this asset?",
  "ui_context": {"selected_ip": "192.0.2.10"}
}
```

`ui_context` may be omitted or `null`; `selected_ip` may be omitted, blank, or
`null`. The request model only trims it. Deterministic entity resolution later
accepts valid IPv4 and ignores malformed or IPv6 values; malformed UI input does
not currently produce a 422. A syntactically valid IP that is absent from the
Graph can still become the target; relevant providers then report not-found or
unavailable evidence.

Entity authority is:

```text
explicit valid IP(s) in the current message
  > valid UI-selected IP
  > active entity/entities in Copilot session memory
```

Consequences:

- An explicit message IP is not replaced by a conflicting selected IP.
- A valid explicit pair is preserved for comparison/path requests.
- Without an explicit IP, referential text such as "what is this?" can use the
  UI-selected IP.
- Pair references can use a previous active pair when no explicit/UI entity has
  higher authority.
- Topic-detached general questions suppress stale asset context.
- Browser deselection should set local selected state to null and send
  `ui_context: null` (omission is equivalent in the current API).
- Do not inject the selected IP into the natural-language message; use the typed
  field and keep the user's words unchanged.

Current Streamlit clears selection when empty Graph canvas is clicked. Its
"Clear chat" action creates a new session but intentionally does **not** clear
the selected Graph asset; Product UX should decide whether new-chat should also
clear selection and implement that choice explicitly.

## 11. Exact /chat contract

Request:

```ts
type ChatRequest = {
  session_id?: string | null;       // bounded legacy/runtime continuity ID
  conversation_id?: string | null;  // preferred future durable thread ID
  request_id?: string | null;       // one turn; generated when absent
  message: string;                  // JSON string, min_length=1
  ui_context?: { selected_ip?: string | null } | null;
};
```

Both chat routes also accept optional `X-User-ID` metadata. It is bounded and
validated but never authenticates or authorizes the request.

Successful response, captured locally with the harmless prompt "Just say test.":

```http
HTTP/1.1 200 OK
content-type: application/json

{"status":"ok","data":{"session_id":"frontend-contract-test","answer":"test","provider":"arvan","model":"kimi-k3"},"warnings":[],"errors":[]}
```

No `request_id`, `trace_id`, resolved entities, evidence pack, or provider
limitations are returned. Final answer text is `data.answer`. Provider and model
name are returned, but provider evidence status is not.

Handled model error:

```json
{
  "status": "error",
  "data": null,
  "warnings": [],
  "errors": [{"reason": "provider_reason", "message": "safe message"}]
}
```

This handled error remains HTTP 200, so clients must inspect `status`.

## 12. Exact /chat/stream wire protocol

The response is standards-shaped Server-Sent Events (SSE), not NDJSON, JSON
Lines, or raw token text.

Verified response headers:

```http
HTTP/1.1 200 OK
content-type: text/event-stream; charset=utf-8
cache-control: no-cache
x-accel-buffering: no
transfer-encoding: chunked
```

Each record is exactly:

```text
event: <event-type>\n
data: <compact UTF-8 JSON object>\n
\n
```

Example captured sequence (token counts intentionally illustrative here; their
shape comes from provider-reported usage and may contain additional keys):

```text
event: answer_delta
data: {"type":"answer_delta","text":"test"}

event: usage
data: {"type":"usage","data":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}

event: done
data: {"type":"done","data":{"session_id":"frontend-stream-contract-test","provider":"arvan","model":"kimi-k3","warnings":[]}}

```

Public event union:

| Event | JSON fields | Meaning |
|---|---|---|
| `reasoning_delta` | `type`, `text` | only sent when reasoning exposure is enabled; normally absent |
| `answer_delta` | `type`, `text` | append text exactly, preserving Unicode |
| `usage` | `type`, `data` | provider-reported usage; flexible metadata, not a stable billing schema |
| `done` | `type`, `data.session_id`, `provider`, `model`, `warnings` | only successful application-level completion marker |
| `error` | `type`, `message`, `data.reason` | safe failure after HTTP 200; no `done` follows |

There are no SSE comments or heartbeats. `session_id` appears only in `done`.
No request ID or trace ID appears in-stream.

If the provider stream fails before emitting answer text, Copilot may perform a
non-stream model fallback and emit one answer delta followed by `done`. If it
fails after partial answer output, Copilot emits `error`; clients must retain or
mark the partial output as interrupted and must not treat it as complete.

Browser disconnect currently stops delivery but is not propagated to the daemon
workflow/model worker. It may continue consuming resources. `AbortController`
still matters for UX, but true upstream cancellation is a required follow-up.

## 13. Current Streamlit implementation

The reference UI in `app/app_st.py`:

- creates `uuid4().hex` once in `st.session_state.session_id`;
- reuses it for all messages until "Clear chat";
- stores display history in `st.session_state.messages`;
- sends only the current message, not browser history;
- relies on Copilot's session memory for continuity;
- includes `ui_context` only when a Graph IP is selected;
- uses `/chat/stream` for the normal UI path;
- has a non-stream `/chat` helper that is not used by the rendered chat flow;
- sets response encoding to UTF-8 and consumes `iter_lines(..., decode_unicode=True)`;
- parses `data:` JSON, ignores comments and the `event:` line, and uses JSON
  `type` as the event discriminator;
- accumulates answer deltas, stores the assistant message only after `done`, and
  displays but does not store a partial answer when interrupted;
- checks `/health` and `/llm/health` for sidebar status;
- loads the local Graph pickle directly and calls Python Graph helpers;
- does **not** call the public `/graph/*` endpoints;
- supports PyVis node select, empty-canvas clear, Explore-IP select, zoom, and
  local Graph filters;
- automatically attaches `Authorization: Bearer <SOORIN_COPILOT_API_KEY>` to every
  protected request (`/chat`, `/chat/stream`, `/llm/health`) and omits the header
  from the unauthenticated `/health` endpoint;
- shows a sidebar warning when `SOORIN_COPILOT_API_KEY` is not configured.

Streamlit-specific behavior that must not be copied: direct pickle access,
in-process Graph functions, Streamlit session state as durable history, PyVis's
custom component protocol, rerun-based rendering, and local API URLs. A real
browser should use Product-owned state and HTTP APIs.

Audit note: the current Streamlit history loop renders `st.markdown` twice for
each stored item. This is a UI defect outside this contract task, not an API
contract to reproduce.

## 14. Production frontend state machine

```text
IDLE
  -> SUBMITTING: append user message once, disable duplicate submit
  -> STREAMING: append answer_delta; optionally show reasoning_delta separately
  -> COMPLETE: on done, commit assistant message and returned session_id
  -> FAILED_BEFORE_TEXT: show retryable error
  -> INTERRUPTED_AFTER_TEXT: preserve partial display, mark incomplete, no auto retry
  -> CANCELLED: abort browser request and mark local turn cancelled
```

The frontend should retain Product display history independently of Copilot
runtime memory. It should not resend full history unless a future API contract
adds such a field.

## 15. TypeScript contracts

These interfaces mirror current response fields, including nullable and
variable health metadata:

```ts
export interface ChatUIContext {
  selected_ip?: string | null;
}

export interface ChatRequest {
  session_id?: string | null;
  conversation_id?: string | null;
  request_id?: string | null;
  message: string;
  ui_context?: ChatUIContext | null;
}

export interface ChatData {
  session_id: string;
  answer: string;
  provider: string;
  model: string;
}

export interface ChatErrorItem {
  reason: string;
  message: string;
}

export type ChatResponse = {
  status: "ok" | "error";
  data: ChatData | null;
  warnings: string[];
  errors: ChatErrorItem[];
};

export type ChatStreamEvent =
  | { type: "reasoning_delta"; text: string }
  | { type: "answer_delta"; text: string }
  | { type: "usage"; data: Record<string, unknown> }
  | { type: "done"; data: {
      session_id: string; provider: string; model: string; warnings: string[];
    } }
  | { type: "error"; message: string; data: { reason: string } };

export interface GraphDegree {
  in: number;
  out: number;
  total: number;
}

export interface GraphStatusResponse {
  loaded: boolean;
  nodes: number;
  edges: number;
  directed: boolean;
  artifact_available: boolean;
  active_graph_loaded_at: string | null;
  active_graph_source: string | null;
  active_graph_version: string | null;
  refresh_enabled: boolean;
  refresh_running: boolean;
  refresh_interval_seconds: number;
  refresh_last_attempt_at: string | null;
  refresh_last_success_at: string | null;
  refresh_last_failure_at: string | null;
  refresh_last_error_type: string | null;
  refresh_last_error_message: string | null;
  refresh_consecutive_failures: number;
  raw_snapshot_path: string | null;
  processed_snapshot_path: string | null;
  last_known_good: boolean;
}

export interface GraphStatsResponse {
  total_nodes: number;
  total_edges: number;
  avg_degree: number;
  top_destinations: Array<{ ip: string; incoming: number }>;
  top_sources: Array<{ ip: string; outgoing: number }>;
  ip_range_distribution: Record<string, number>;
}

export interface GraphNodeResponse {
  ip: string;
  found: boolean;
  degree: GraphDegree;
}

export interface GraphNeighbor {
  ip: string;
  direction: "in" | "out";
  edge_weight: number;
}

export interface GraphNeighborsResponse {
  target_ip: string;
  found: boolean;
  direction: "in" | "out" | "both";
  total: number;
  returned: number;
  neighbors: GraphNeighbor[];
}

export interface GraphContextResponse {
  target_ip: string;
  node_found: boolean;
  degree: GraphDegree;
  top_inbound_peers: string[];
  top_outbound_peers: string[];
  bidirectional_peers: string[];
  subnets_reached: string[];
  limitations: string[];
}

export interface GraphPathResponse {
  source: string;
  target: string;
  found: boolean;
  path: string[];
  edge_count: number;
  semantics: string;
  reason: "source_not_found" | "target_not_found" |
    "no_observed_communication_graph_path" | null;
}

export interface ValidationIssue {
  loc: Array<string | number>;
  msg: string;
  type: string;
  input?: unknown;
  ctx?: Record<string, unknown>;
}

export type ValidationErrorResponse =
  | { detail: ValidationIssue[] }
  | { detail: string };
```

OpenAPI is now sufficiently typed for ordinary JSON response generation, but
the SSE union and incremental parser still require handwritten frontend code.
The LLM usage event and nested deployment diagnostics intentionally remain
extensible.

## 16. Streaming parser example

`EventSource` cannot POST a JSON body, so use `fetch()` and parse SSE framing.
This parser keeps a persistent `TextDecoder`, handles UTF-8 characters split
across transport chunks, supports multiple `data:` lines, and validates JSON
`type` rather than assuming network chunk boundaries equal events.

```ts
export async function streamCopilot(
  url: string,
  request: ChatRequest,
  signal: AbortSignal,
  onEvent: (event: ChatStreamEvent) => void,
): Promise<void> {
  const response = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify(request),
    signal,
  });

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new HttpError(response.status, body);
  }
  if (!response.headers.get("content-type")?.startsWith("text/event-stream")) {
    throw new Error("Copilot returned an unexpected content type");
  }
  if (!response.body) throw new Error("Streaming body is unavailable");

  const decoder = new TextDecoder("utf-8", { fatal: false });
  const reader = response.body.getReader();
  let pending = "";
  let dataLines: string[] = [];

  const consumeLine = (line: string) => {
    if (line.endsWith("\r")) line = line.slice(0, -1);
    if (line === "") {
      if (dataLines.length) {
        const value = JSON.parse(dataLines.join("\n")) as ChatStreamEvent;
        if (!value || typeof value.type !== "string") {
          throw new Error("Invalid Copilot stream event");
        }
        onEvent(value);
        dataLines = [];
      }
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
    // Current Streamlit likewise ignores comments and the event: line.
  };

  while (true) {
    const { value, done } = await reader.read();
    pending += decoder.decode(value ?? new Uint8Array(), { stream: !done });
    let newline: number;
    while ((newline = pending.indexOf("\n")) >= 0) {
      consumeLine(pending.slice(0, newline));
      pending = pending.slice(newline + 1);
    }
    if (done) break;
  }
  pending += decoder.decode();
  if (pending) consumeLine(pending);
  consumeLine("");
}
```

On `answer_delta`, append `text`. On `done`, mark complete and update the local
session ID from `data.session_id`. On `error`, mark the turn failed. Never render
reasoning as final answer text.

## 17. Cancellation and retry behavior

- Create one `AbortController` per submitted turn.
- Cancel on explicit user action, page teardown, or conversation switch.
- Current Copilot does not propagate disconnect to its worker/model call, so
  cancellation is client-side delivery cancellation, not guaranteed compute
  cancellation.
- Retry transport errors only before any answer delta was displayed.
- After partial text, require an explicit user retry and create a new turn; an
  automatic replay can duplicate model work and token usage.
- A retry should use the same Product request idempotency/correlation policy,
  but do not assume Copilot chat itself is idempotent.
- Do not fall back to `/chat` after partial SSE output.

## 18. Product persistence responsibilities

The current Product Backend does not proxy Copilot. It owns Product users,
chatrooms, chatroom ownership, persisted user/assistant messages, timestamps,
and Product retention/deletion policy. The Frontend creates or opens a chatroom,
calls Copilot directly, receives SSE directly, and stores the completed turn
through existing Product Backend message endpoints. Only a completed assistant
message after SSE `done` should be stored as complete; interrupted output may be
stored separately according to Product policy.

## 19. Optional gateway hardening

A future gateway or BFF is optional security hardening, not a current Product
requirement. If adopted, it can remove the static Copilot key from browser code,
enforce per-user/asset authorization and rate limits, propagate correlation and
disconnect signals, and stream SSE without buffering. This optional deployment
must preserve the existing Copilot `/chat/stream` wire contract and must not
route Copilot's internal Product profile/detection/login calls back into itself.

## 20. Authentication and authorization

Copilot protected endpoints (`/chat`, `/chat/stream`, `/llm/health`, and
all `/graph/*` routes) require `SOORIN_COPILOT_API_KEY`. They accept either
`Authorization: Bearer <SOORIN_COPILOT_API_KEY>` or, for a Product request
that retains its Product JWT in `Authorization`,
`Soorin_copilot_api_key: <SOORIN_COPILOT_API_KEY>`. The custom header takes
precedence and an invalid custom key is rejected even when a Bearer credential
is also supplied. The built-in Streamlit UI (`app/app_st.py`) uses the Bearer
form for its protected requests and omits it from `GET /health`. Missing or
invalid Copilot credentials return `401`. This is a service-to-service auth layer only. The Copilot request
still carries no user ID, tenant ID, organization ID, role, permission, or
authorized-asset scope. Therefore these endpoints must not be internet/browser
exposed as an authorization boundary.

## 21. Multi-tenant concerns

The request contract carries no tenant/user identity and memory is keyed only by
session string. If two users submit the same ID to one process, they can share
routing/conversation state. UUID randomness reduces accidental collision but is
not authorization.

Before multi-tenant production:

- bind Product thread IDs to authenticated user and tenant in Product DB;
- enforce that binding on Product chatroom/message requests;
- pass typed conversation/user identity to Copilot without treating it as auth;
- protect direct Copilot access and rotate the browser-visible static key;
- define tenant-correct Product service credentials and Qdrant/Graph data scope;
- decide whether future signed tenant/user context or an optional gateway is required;
- avoid multiple stateless Copilot replicas unless routing is sticky or runtime
  state is moved to a shared, tenant-scoped store.

## 22. Graph API usage

Graph data is a directed NetworkX snapshot built from validated Product unique
IP-pair records. It is not a packet capture, routing table, reachability proof,
or necessarily the same instant as Product UI data.

Detailed behavior:

- Node/path inputs use `ipaddress.ip_address`; syntactic IPv4 and IPv6 are
  accepted at API validation, though the current graph/entity baseline is IPv4.
- Missing nodes return HTTP 200 and `found=false` for node/neighbors/context.
- Neighbor direction is `in`, `out`, or `both`.
- Neighbor records sort by descending `edge_weight`, then IP, then direction.
- `both` can return the same peer twice when both directed edges exist.
- `edge_weight` is duplicate validated topology-record count, not bytes, flow
  frequency, risk, or traffic volume.
- Context returns fixed top inbound/outbound/bidirectional peer lists, subnet
  list, and limitations.
- Path is NetworkX directed shortest path in observed communication edges.
- Path `reason` can be `source_not_found`, `target_not_found`, or
  `no_observed_communication_graph_path`.
- Equal source/target currently returns `found=true`, one-node path, even if the
  node is absent; frontend should avoid using that as existence proof.
- Graph version/freshness is exposed by `/graph/status`; last-known-good can
  remain loaded after refresh failure.

## 23. Graph pagination and truncation

`/graph/nodes/{ip}/neighbors` has `limit` but no `offset`, cursor, continuation
token, or stable snapshot token. `total` is the full matching record count and
`returned` is the included count. When `returned < total`, show "showing N of
M" and do not describe the list as complete.

The API is not sufficient for production page-through pagination. Increasing
`limit` can retrieve more up to the configured maximum (default 1000), but that
is bounded expansion, not pagination. Add snapshot/version-bound cursor or
offset semantics before using it for very large browser lists.

Stats top source/destination lists are capped at 10. Context peer lists are
internally capped (currently 20 per inbound/outbound direction). Neither offers
pagination.

## 24. Error handling

Frontend handling matrix:

| Condition | Current signal | Action |
|---|---|---|
| invalid JSON/body/query | HTTP 422, `detail` string or array | show field-safe validation; do not retry unchanged |
| Product API unauthenticated/unauthorized | Product 401/403 | reauthenticate or show access denied |
| quota/rate limit | recommended 429 | honor `Retry-After`; no immediate loop |
| Copilot unavailable | network/5xx | availability UI; retry only before output |
| handled non-stream LLM failure | HTTP 200 with `status=error` | inspect body status, show safe error |
| stream model failure | HTTP 200 then SSE `error` | mark interrupted; no `done`; do not auto replay after partial text |
| browser abort | `AbortError` | mark cancelled, not failed |
| missing Graph node | HTTP 200 `found=false` | normal empty/not-found state |
| Graph truncation | `returned < total` | visibly label partial result |

Never render backend error text as raw HTML. Bound diagnostic display and keep
full exception details server-side.

## 25. Request IDs and observability

Copilot generates an internal 12-hex request ID for each `/chat` or stream call
and a separate internal trace ID, but neither is currently returned in JSON,
SSE, or headers. Therefore a Product request ID cannot yet be correlated to
Copilot logs at wire level.

Immediate Frontend/Product behavior:

- generate/preserve a Product request ID for Product message records;
- log safe status, first-byte time, completion/error event, and disconnect;
- never log secrets, authorization headers, raw Product evidence, hidden
  reasoning, or full model responses.

Recommended bounded Copilot follow-up: accept/validate a correlation header or
return its internal request ID in a response header and final/error metadata.
That is not implemented in this audit to avoid changing the runtime contract.

## 26. Chat persistence responsibilities

Current Copilot memory is bounded, in-process memory. It stores recent raw
user/assistant turns, compact summaries/episodes, and active routing entities.
It is not durable and is lost on restart. The Streamlit UI separately stores
display messages only in Streamlit session state. No Product database chat
persistence was found in this repository.

Recommended division:

```text
Product database
  conversation metadata and title
  user and tenant ownership
  displayed user/assistant messages
  timestamps and completion/cancellation state
  Product request/correlation IDs

Copilot runtime
  bounded active entity and routing/workflow memory
  compact short-term context
  no claim of durable chat history
```

Persist only the final committed assistant message after `done`; store partial
output separately as interrupted if Product policy requires it.

## 27. Postman usage

Use:

- `postman/Soorin_Copilot_API.postman_collection.json`
- `postman/Soorin_Copilot_Local.postman_environment.json`

Set variables for non-local environments through a private, untracked Postman
environment. The tracked environment contains no credentials or organization
asset IPs. Postman may buffer or display SSE differently from a browser and is
not proof of proxy buffering behavior; use `curl -N` or browser integration for
wire-level stream QA.

The collection is for contract exploration and QA. It does not implement
Product chatroom/message persistence or user authorization.

## 28. Deployment/service discovery

Local example base URL is `http://127.0.0.1:6998`. Compose API service name is
`api` on `copilot-network`, while an external Product stack should use its own
internal DNS/service discovery and health policy. Do not bake container names or
host loopback addresses into frontend bundles.

The API healthcheck currently checks `/health` and `/openapi.json`; this verifies
process/schema availability, not models, Product API, Graph freshness, or RAG.
Use `/llm/health` for deployment configuration readiness and `/graph/status` for
Graph diagnostics, with backend-only access.

Qdrant local mode and in-process session memory constrain horizontal scaling.
Do not point multiple writers at one embedded local Qdrant path, and do not
assume conversation continuity across replicas.

## 29. Authentication configuration

### Configuration variable

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SOORIN_COPILOT_API_KEY` | yes | (none) | Static Copilot API key for service-to-service auth |

Set in `app/.env`:

```ini
SOORIN_COPILOT_API_KEY=your-generated-api-key
```

### Which endpoints are protected

Every endpoint except `GET /health` requires the API key:

| Protected | Endpoint |
|---|---|
| Yes | `GET /llm/health` |
| Yes | `POST /chat` |
| Yes | `POST /chat/stream` |
| Yes | `GET /graph/status` |
| Yes | `GET /graph/stats` |
| Yes | `GET /graph/nodes/{ip}` |
| Yes | `GET /graph/nodes/{ip}/neighbors` |
| Yes | `GET /graph/nodes/{ip}/context` |
| Yes | `GET /graph/path` |
| **No** | **`GET /health`** |

### HTTP responses

| Condition | Status | Body |
|---|---|---|
| Missing Bearer and custom Copilot credentials | `401` | `{"detail":"Missing Authorization header"}` |
| Invalid Bearer or custom Copilot key | `401` | `{"detail":"Invalid Bearer token"}` |
| Valid Bearer or custom Copilot key | `200` | Normal response |

### Development workflow

```text
# Generate an API key (one time)
SOORIN_COPILOT_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

# Start the API
SOORIN_COPILOT_API_KEY=$SOORIN_COPILOT_API_KEY python app/run.py --api

# Test the key
curl -H "Authorization: Bearer $SOORIN_COPILOT_API_KEY" http://127.0.0.1:6998/chat/stream
```

### Current Product workflow

```text
Browser
  |  1. Authenticate with Product and create/open a Product chatroom
  |  2. POST directly to Copilot /chat/stream
  |  3. Keep Product JWT in Authorization and add Soorin_copilot_api_key,
  |     or use the Copilot Bearer form when no Product JWT is present
  v
Soorin Copilot API
  |  Validates the custom Copilot key when present; otherwise the Bearer key
  |  Rejects with 401 if missing or invalid
  |  Streams SSE directly to Browser
  v
Browser
  |  4. Persist user and completed assistant messages
  v
Product Backend chatroom/message endpoints -> Product PostgreSQL
```

### Example Authorization header

```http
Authorization: Bearer abc123def456ghi789jkl012mno345pqr678stu901vwx
```

The Bearer token is the literal value of `SOORIN_COPILOT_API_KEY`. The current
direct-browser integration necessarily exposes this shared key to browser code;
it is not user authorization and should be treated as a temporary limitation.

When the Product request keeps its own JWT in `Authorization`, send instead:

```http
Soorin_copilot_api_key: abc123def456ghi789jkl012mno345pqr678stu901vwx
```

This is a Copilot service credential, not a replacement for the Product JWT.

### OpenAPI / Swagger

Protected endpoints expose an **Authorize** button in Swagger UI
(`/docs`). Clicking it prompts for a Bearer token. All protected operations
then include the `Authorization: Bearer <token>` header automatically.

### Implementation notes

- Uses FastAPI `HTTPBearer` security scheme with `Security()` dependency.
- `GET /health` intentionally excluded — used by load balancers, healthchecks,
  and monitoring without requiring the API key.
- No user, tenant, or role scoping — this is a single shared static key.
- The key is loaded from `app/.env` at settings initialization time.
- Changing the key requires a service restart.

## 30. Current limitations

- no tenant/user authorization (static API key only);
- session IDs are arbitrary, unbounded strings and are not identity scoped;
- raw session/episode memory does not survive restart or safely span replicas;
- optional local SQLite routing continuity is development-only and is not a
  production ownership, authorization, or multi-replica solution;
- browser disconnect does not cancel upstream model/workflow execution;
- no heartbeats during long pre-answer periods;
- no public request/trace ID;
- handled `/chat` LLM errors use HTTP 200;
- `/chat` returns no evidence status, resolved entities, citations, or limitations;
- SSE `usage.data` is provider-flexible rather than a generated-client contract;
- Graph status exposes filesystem paths;
- Graph neighbors have truncation but no pagination;
- Product live Graph and Copilot snapshot Graph can differ;
- selected IP request validation trims but does not return 422 for malformed IP;
- Streamlit directly reads local Graph artifacts and is not a production frontend pattern;
- current Streamlit history display duplicates each stored message visually;
- OpenAPI cannot generate the incremental stream parser.

## 31. Required follow-up changes

Before broad frontend release, Product and Copilot teams should decide and then
implement, in priority order:

1. Product chatroom/message integration and conversation ownership validation.
2. Context compaction and durable thread state.
3. Trusted Product ownership validation for identity metadata.
4. Decide whether an optional gateway is needed to remove the static key from
   browser code and add stronger asset authorization/rate limits.
4. True disconnect/cancellation propagation and optional SSE heartbeat policy.
5. Whether Product canonical Graph or Copilot snapshot Graph owns each UI panel.
6. Cursor/version pagination if browser Graph neighbor lists require page-through.
7. Remove or role-filter filesystem path diagnostics from user-facing status.
8. Optional API hardening: strict IPv4 UI-context validation, coordinated as a
   versioned behavior change.
9. Decide whether evidence/limitations/citations need a stable public response
   contract rather than remaining inside answer prose.

The current direct streaming path remains valid with the optional identifier
fields and dormant storage ports introduced backward-compatibly.

## 32. Frontend implementation checklist

- [ ] Call Copilot `/chat/stream` directly; never call internal provider URLs.
- [ ] Create or obtain one authorized UUID per chat and reuse it.
- [ ] Keep Product display history; send only current `message`.
- [ ] Send `ui_context.selected_ip` as typed state, not injected prose.
- [ ] Clear/send null on deselection; decide new-chat selection behavior.
- [ ] Use `POST /chat/stream` with `Accept: text/event-stream`.
- [ ] Parse SSE across arbitrary byte/chunk boundaries with UTF-8 `TextDecoder`.
- [ ] Append only `answer_delta`; finalize only on `done`.
- [ ] Treat `error` after 200 as failure and preserve partial text as incomplete.
- [ ] Add `AbortController`; do not auto retry after partial output.
- [ ] Handle 422, 401/403, 429, and 502/503/504 distinctly.
- [ ] Sanitize Markdown: disable raw HTML, unsafe links, scripts, and event handlers.
- [ ] Show Graph `returned/total` truncation and path semantics.
- [ ] Persist final messages only under authenticated Product ownership.

## 33. Product Backend integration checklist

- [ ] Keep Product user, chatroom, message, retention, and deletion ownership.
- [ ] Enforce user/chatroom ownership for every Product message operation.
- [ ] Persist the user message and only the completed assistant message after `done`.
- [ ] Preserve Product request/correlation IDs with persisted turns.
- [ ] Keep Product/API/model credentials out of logs.
- [ ] Decide separately whether an optional Copilot gateway is required.

## 34. QA acceptance checklist

- [ ] `/health` returns exact 200 JSON status without any Authorization header.
- [ ] `/llm/health` returns 401 without Authorization header.
- [ ] `/llm/health` returns 401 with invalid Bearer token.
- [ ] `/llm/health` returns 200 with valid Bearer token.
- [ ] `/chat` success and handled error envelopes match this document.
- [ ] `/chat/stream` is UTF-8 SSE with blank-line frame boundaries.
- [ ] Unicode smart quotes, Persian, arrows, dashes, and emoji round-trip exactly.
- [ ] A stream completes only on `done`; an `error` after 200 is surfaced.
- [ ] Browser/parser works when one UTF-8 character spans network chunks.
- [ ] One chat reuses one authorized session ID; new chat uses a new ID.
- [ ] Two users cannot access one another's session by guessing/submitting an ID.
- [ ] Explicit message IP wins over a conflicting selected IP, after both are
      authorized by Product backend.
- [ ] Deselect sends null/omits context and no stale selected IP is forwarded.
- [ ] General detached questions do not receive stale asset context.
- [ ] Browser cancel stops rendering and is recorded as cancelled.
- [ ] Direct browser stream emits chunks promptly without client-side buffering.
- [ ] 422, 401/403, 429, 502/503/504, partial stream, and disconnect are tested.
- [ ] Missing Graph node is a normal 200 not-found state.
- [ ] Neighbor truncation is visible and no unsupported pagination is implied.
- [ ] Product and Copilot Graph freshness/version differences are tested.
- [ ] Postman files contain no credentials or organization-specific IPs.
- [ ] Generated OpenAPI JSON contracts pass, while SSE uses the manual parser.
