# Phase 4 Structured Graph Routing

Status: current `dev` implementation record for Phase 4A.1 through Phase 4B.4.

## Current boundary

Phase 4A provides typed, bounded, active-version-only Neo4j Asset search and aggregation. Phase 4A.1 adds external cursor request/version binding plus a read-only query-plan audit. Phase 4B.1 adds the semantic contract for structured Asset-set requests. Phase 4B.2 connects that contract to bounded agent execution. Phase 4B.3 carries those results through typed evidence, deterministic review, bounded context, and task-specific synthesis. Phase 4B.4 adds bounded, thread-scoped continuity for a later semantic reference to that query/result.

No Product backend, frontend, public API, PostgreSQL schema, Qdrant schema, or Neo4j schema is changed. Phase 4B.4 bumps only the internal ThreadState JSON contract from 4 to 5 while retaining version 3/4 readers; Product continues storing the same bounded `stateJson`.

## Semantic contract

```text
Asset/IP                         conversational entity
role/vendor/product/status/...  structured selector
structured query result         Asset set
```

New semantic intents:

```text
asset_search
asset_aggregate
```

The Router may attach one validated `StructuredQuerySpec` using the same Phase 4A allow-lists. The contract contains no Cypher, arbitrary property names, regex, OR expression, traversal instruction, or free-form operator.

For set queries:

```text
scope = none
direction = none
depth = 0
requires_graph = true
entity_binding = none
requires_multiple_entities = false
```

Profile and Detection are not requested at this stage because no focal Asset has yet been selected. Later cross-source deepening remains bounded to selected focal Assets.

`IntentDecision` and `RouteDecision` carry the structured semantic object, and `TaskSpec.structured_query` remains the single semantic bridge into execution. The deterministic direct-plan compiler validates the intent/mode invariant and produces exactly one `graph.search_assets` or `graph.aggregate_assets` step with mode-specific typed arguments.

Malformed or contradictory structured tasks fail closed. Existing topology primitives are not substitutes for Asset-set execution.

## Phase 4B.2 execution contract

```text
asset_search / asset_aggregate
→ StructuredQuerySpec
→ TaskSpec.structured_query
→ deterministic Direct Plan (simple requests) or bounded Planner catalog
→ PlanValidator
→ Graph Specialist
→ CapabilityExecutor
→ graph.search_assets / graph.aggregate_assets
→ GraphService
→ Neo4jGraphRepository active projection
```

Both capabilities are read-only, planner-visible, depth zero, and require exactly zero focal entities. Their planner schemas expose only:

```text
search:    filters, sort, direction, limit
aggregate: filters, operation, group_by, limit
```

They reuse the Phase 4A selector and enum contracts, accept no cursor or arbitrary query/property/operator fields, and enforce the configured structured-result maximum during plan validation. One Asset-set retrieval remains one capability call regardless of matched row count; returned rows never enter `TaskSpec.entities`, active entities, or the two-entity budget.

The Graph Specialist remains a deterministic bounded subgraph, not an LLM agent. Existing generic `ToolResult` fields carry the typed result and bounded count/truncation metadata. Asset-set-aware EvidencePack and model-context semantics are intentionally not introduced here.

## Phase 4B.3 evidence-to-answer contract

```text
graph.search_assets / graph.aggregate_assets
→ typed ToolResult Asset-set evidence
→ EvidencePack
→ deterministic EvidenceReviewer
→ dedicated bounded ContextComposer serialization
→ deterministic Synthesizer task module
→ existing Synthesizer transport
```

Search and aggregate use distinct evidence classes. Their query identity is a canonical SHA-256 fingerprint of normalized mode/filter/sort/direction or operation/grouping semantics plus the active graph version; retrieval limits and timestamps are intentionally excluded. EvidencePack retains the typed object and its Neo4j organizational-projection provenance.

The Reviewer accepts zero focal entities and treats one capability result as one set receipt. It validates capability, mode, query identity, graph version, provenance, counts, groups, and truncation locally. A valid empty search or zero aggregate is sufficient. A valid truncated result remains answerable with limitations. Missing evidence can use the existing single supplemental Graph retrieval; no returned row triggers a Product or Detection call.

Context composition uses a dedicated structured serializer. Search retrieval may retain up to the configured retrieval limit, while model context includes at most 20 rows and may include fewer under the existing token budget and output reservation. Identity, requested selector fields, sort field, and a stable allow-listed analyst projection are retained; individual strings and list values are bounded. Aggregate evidence uses a smaller context cap and preserves the total even when grouped output is context-bounded. The model receives explicit retrieval-versus-context counts, truncation state, limitations, and projection authority.

Synthesizer module selection adds no model call. `asset_search` and `asset_aggregate` modules require direct grounded answers, explicit subset caveats, valid-zero handling, and no invented Product truth. Selective cross-source deepening remains deferred to Phase 4C.

## Phase 4B.4 structured continuity contract

```text
reviewed structured ToolResult
→ update_memory
→ bounded StructuredQueryContext
→ SessionRoutingState / ThreadMemoryState v5
→ existing owner-scoped ThreadStateStore
→ next request Router summary
→ typed semantic reference
→ deterministic materialization
```

`StructuredQueryContext` retains the canonical query and query identity, active graph version, result counts, retrieval truncation, source/timestamps, and no more than eight ordered `ip`/optional `graph_key`/short-name refs for search or eight label/count groups for aggregation. Eight is independent of the 50/200 retrieval bounds and leaves room inside the established 16,384-byte total ThreadState envelope. It never stores complete Asset rows, classification summaries, provider payloads, or assistant prose.

Its `structured-query-context:v1` SHA-256 fingerprint binds the mode, canonical query identity, graph version, ordered bounded refs or groups, counts, and retrieval/continuity truncation. Restore recomputes both query identity and result fingerprint. Malformed optional continuity is dropped without invalidating established thread state; under total-size pressure refs are reduced and then the context is removed before working memory, turn references, episodes, active entities, or explicit facts are harmed.

There are two current-follow-up families:

- Set-level continuity: the Router emits a full allow-listed `StructuredQuerySpec` inheriting the intended prior selectors and applying a count/group/sort/filter refinement. The normal direct Graph capability reruns against the current active projection; retained counts/refs never substitute for current evidence.
- Entity-selection continuity: a typed one-based selection chooses at most one or two retained ordered search refs. Deterministic code validates the selection and then feeds those IPs into the existing focal-entity or comparison workflow. No search row becomes active before that later selection.

Historical result recall reuses the existing memory-only answer path with an explicit “historical structured-query continuity, not current operational evidence” label. It exposes only retained refs/groups and discloses bounded/truncated state. A graph-version change does not destroy an IP referent, but any current claim still requires current Graph/Product/Detection evidence. Gate 8 does not treat the context as evidence, and it is never promoted to Working Facts, baselines, Product LTM, or Qdrant.

Natural references are classified by the existing Semantic Router using a small bounded summary. There is no fixed English phrase catalogue and no extra model call. The Router emits only `none`, `set_query`, `select_entities`, or `historical_recall`; deterministic validation owns context availability, explicit-message precedence, maximum-two cardinality, canonical IPs, ordinals, query schema, identity, and fingerprint. Repair accepts the same schema. Unavailable/invalid selections clarify rather than falling through to UI or active entities; ordinary vague pronouns and EntityVisit ordinals keep their established namespaces.

Unrelated and DETACH turns neither bind this context implicitly nor erase the latest bounded thread-scoped reference. A clear set reference can outrank stale UI/active fallback, but an explicit message IP or pair remains authoritative. No automatic Product fan-out or Phase 4C candidate selection is performed.

## Router prompt policy

The Router prompt is extended only with the minimum structured-query rules:

- distinguish a known focal Asset from an Asset-set request;
- use only Phase 4A selector/sort/group allow-lists;
- keep properties as selectors rather than entities;
- emit `structured_query=null` for ordinary routes;
- never emit Cypher or arbitrary operators;
- do not request Product Profile/Detection before a focal Asset is selected.

No new LLM role is introduced. The same semantic Router remains responsible for classification; deterministic Pydantic validation owns the contract.

## Phase 4A.1 measurement gate

`app/scripts/audit_phase4a1_query_plans.py` is read-only. It inspects the currently published graph and can run `EXPLAIN` plus optional `PROFILE` for representative IP, role, product, vendor, status, confidence, enrichment-status, and `matched_total` queries. It never bootstraps schema, publishes snapshots, writes Assets, or creates indexes.

Run against a real read-only/current Neo4j projection when production-representative query plans are desired:

```bash
PYTHONPATH=app SOORIN_PHASE4A1_PROFILE=1 \
.venv/bin/python app/scripts/audit_phase4a1_query_plans.py
```

`PROFILE` executes the read query but performs no mutation. Index changes remain manual and require measured evidence.

The mutating Community regression tests still require a disposable Neo4j instance. They must never target the main runtime database.

## CI gate

`.github/workflows/phase4-validation.yml` creates a disposable Neo4j Community service on pushes to `dev`, then runs:

1. compileall;
2. Phase 4 offline contracts;
3. structured Neo4j integration regression;
4. read-only EXPLAIN/PROFILE audit;
5. existing Neo4j Community regression.

This gives the repository an isolated database validation path without requiring the developer's real Neo4j instance.

## Scale items

Two measured decisions remain intentionally separate from semantic routing:

- `matched_total` currently costs an extra count query. Keep the contract until PROFILE evidence demonstrates a material problem.
- `/graph/stats` currently derives IP-range distribution from a full active-IP materialization. This is a known scale debt for the 1M-Asset target and should be refactored after profiling that public stats path, preserving its response contract.

## Next phases

Phase 4B.3 set-aware EvidencePack/Reviewer/ContextComposer/Synthesizer handling and Phase 4B.4 bounded result-set continuity are implemented. Phase 4C adds automatic selective cross-source Product/Detection/topology deepening; Phase 4B.4 performs no such fan-out.
