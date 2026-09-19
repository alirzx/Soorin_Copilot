# Frontend / Backend Integration for Soorin Copilot

This document defines the current integration contract between the Soorin Product frontend, Copilot API/UI, and Product Backend services.

## Integration Model

Soorin Copilot can be used through the Streamlit interface during development and through the Product frontend/API integration in the deployed platform.

The frontend is responsible for user/session context and may provide a selected asset. Copilot remains responsible for request interpretation, bounded entity resolution, evidence retrieval, graph analysis, memory continuity, and synthesis.

## Copilot API

The FastAPI service listens on container port `6998`.

Default host deployment port:

```text
6998
```

The public health endpoint is:

```http
GET /health
```

OpenAPI is available at:

```http
GET /openapi.json
```

Other protected Copilot endpoints require configured Copilot authentication.

## Copilot Authentication

The API supports the established dual-header contract:

1. preferred dedicated Copilot API-key header:

```text
Soorin_copilot_api_key: <configured key>
```

2. compatible Bearer fallback:

```text
Authorization: Bearer <configured key>
```

The same configured `SOORIN_COPILOT_API_KEY` protects authenticated Copilot routes. Frontend and monitoring clients must not embed a different secret contract.

## Conversation and Session Identifiers

Conversation/session identifiers crossing the frontend/Copilot contract should be serialized as strings. Copilot treats the Product conversation ID as the durable thread identifier for transcript and Thread-State integration.

Do not assume the identifier is numerically meaningful inside Copilot. Preserve the exact value supplied by the Product/frontend contract.

## Product Chat Rooms

When Product-backed chat mode is enabled, Copilot uses the configured Product chat-room endpoint:

```text
SOORIN_PRODUCT_CHAT_ROOMS_PATH=/chat-rooms
```

The Product transcript and Copilot Thread-State are separate sources:

```text
Product chat room/messages
→ conversational transcript

Product Copilot Thread-State
→ active entities/pairs, working memory, summaries, episodes,
  baselines, structured-result continuity, and routing state
```

Both can be restored for the same conversation.

## Product Thread-State

Copilot uses the Product memory base path:

```text
SOORIN_PRODUCT_MEMORY_THREAD_STATE_PATH=/api/v1/copilot/memory/thread-state
```

The conversation ID is appended to the configured base path.

The Product wire contract carries the current Product schema version and an opaque `stateJson` document. Copilot's internal memory representation may evolve independently inside that document. Updates use revision-aware persistence so competing writes can be detected.

## Product Long-Term Memory

When production LTM is enabled with the Product backend, Copilot uses:

```text
SOORIN_PRODUCT_MEMORY_LTM_PATH=/api/v1/copilot/memory/ltm
```

Search, record, audit, and transition operations are derived from the canonical Product LTM base path. LTM is user/conversation scoped according to the Product memory contract.

## Selected Asset / UI Context

The frontend may send the currently selected asset/IP as UI context. Copilot combines this with explicit entities found in the current user message and authorized conversation state.

For deterministic user intent, the frontend should preserve exact IP values and send the selected IP whenever the user is intentionally operating on a specific asset. Explicit IPs in user prompts are also resolved directly by the Copilot entity layer.

A structured asset search result is not automatically converted into the UI-selected active asset. Set-level search and single-asset investigation state are distinct concepts.

## Structured Search Results

When a user searches the inventory by role, roles, vendor, product, status, confidence, name, IP, tags, or other supported selectors, Copilot executes a bounded structured Graph request and maintains a `StructuredQueryContext` for result-set continuity.

The frontend should treat the returned answer as an analyst-facing projection rather than as a replacement for the canonical Product asset record.

If a user chooses one result for further investigation, the frontend can select that asset and pass its IP into the next request.

## Deep Asset Investigation

A deep single-asset request can combine:

```text
Product Profile
+ Product Detection
+ Neo4j topology
+ optional Knowledge
+ authorized Memory
```

When structured discovery returns a single unambiguous focal candidate and the request asks for analysis, Copilot may automatically deepen that candidate through the approved Profile, Detection, and Graph capabilities.

## Product Evidence Endpoints

The current Product integration uses configurable paths for:

- topology unique-IP pairs;
- full Asset Profile;
- full Detection;
- Detection overview;
- Detection evidence;
- Detection similarity;
- Detection cluster;
- authentication/login;
- chat rooms;
- Copilot Thread-State;
- Copilot LTM;
- LLM usage reporting.

Exact route templates are configured in `.env` and documented in `.env.example`.

## Product Authentication

Copilot uses the shared Product client for Product evidence, topology refresh, memory integration, and usage reporting.

The client supports configured Product credentials, HWID, token refresh, connect/read timeouts, retry count, and backoff. Expired/invalid authentication is handled according to the Product client policy.

The Copilot API authentication key and Product Backend authentication are separate security domains.

## Streaming Contract

Chat responses use UTF-8 server-sent events. The frontend should consume the event stream incrementally and preserve the final assistant answer after the terminal event.

The stream may contain bounded progress/reasoning-status metadata and usage information according to current API behavior. Provider-internal reasoning is not part of the application contract when `SOORIN_LLM_EXPOSE_REASONING=false`.

Reverse proxies must not buffer the event stream in a way that defeats incremental delivery.

## Timeouts

The frontend/API client timeout is configured separately from internal Router, Planner, Product, agent-executor, and Synthesizer timeouts.

```text
SOORIN_API_TIMEOUT_SECONDS
```

should be large enough to permit the longest intended investigation while internal components continue to enforce their own bounded timeouts.

## Graph API

Copilot exposes authenticated graph functionality backed by the active Neo4j projection. Graph operations are bounded and read-only from the Copilot query surface. The frontend does not need direct Neo4j credentials.

## Streamlit Container Contract

In Compose, the Streamlit service calls:

```text
SOORIN_API_BASE_URL=http://api:6998
SOORIN_COPILOT_API_URL=http://api:6998
```

through Docker service discovery. Host users reach Streamlit through `SOORIN_UI_BIND_IP:SOORIN_UI_HOST_PORT`, normally port `8503`.

## Health Integration

Deployment health validation should check:

```text
GET /health
GET /openapi.json
GET /_stcore/health   # Streamlit host endpoint
```

Protected LLM and graph endpoints should be checked separately with the configured Copilot API authentication after deployment.

## Frontend Data Rules

- Preserve conversation IDs exactly as strings.
- Preserve exact asset IPs as strings.
- Do not reinterpret Copilot's inventory `CONFIRMED` status as the same field as Product operational activity/status.
- Treat Graph discovery fields as the organizational projection and Product Profile/Detection as current deep evidence.
- Keep UI selection explicit and synchronized with the analyst's intended focal asset.
- Do not expose backend Product credentials or LLM provider secrets to the browser.

## Network Deployment

The production frontend may call the Copilot API through an internal gateway/reverse proxy or directly on the configured server interface. API host publishing is controlled by:

```text
SOORIN_API_BIND_IP
SOORIN_API_HOST_PORT
```

Keep non-user-facing services such as Neo4j and local observability endpoints private unless network architecture explicitly requires otherwise.