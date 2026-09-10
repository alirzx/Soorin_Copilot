# Phase 4 Structured Graph Routing

Status: current `dev` implementation record for Phase 4A.1 through Phase 4B.2.

## Current boundary

Phase 4A provides typed, bounded, active-version-only Neo4j Asset search and aggregation. Phase 4A.1 adds external cursor request/version binding plus a read-only query-plan audit. Phase 4B.1 adds the semantic contract for structured Asset-set requests. Phase 4B.2 connects that contract to bounded agent execution.

No Product backend, frontend, public API, PostgreSQL schema, Qdrant schema, Neo4j schema, Memory schema, EvidencePack, ContextComposer, or Synthesizer contract is changed by 4B.2.

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

Phase 4B.3 adds set-aware EvidencePack/Reviewer/ContextComposer and Synthesizer handling. Phase 4B.4 adds bounded short-term result-set continuity. Phase 4C adds selective cross-source Product/Detection/topology deepening; Phase 4B.2 performs no such fan-out.
