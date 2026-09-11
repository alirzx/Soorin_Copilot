# Phase 4 Structured Graph Routing

Status: current `dev` implementation record for Phase 4A.1 through Phase 4C.

## Current boundary

Phase 4A provides typed, bounded, active-version-only Neo4j Asset search and aggregation. Phase 4A.1 adds external cursor request/version binding plus a read-only query-plan audit. Phase 4B.1 adds the semantic contract for structured Asset-set requests. Phase 4B.2 connects that contract to bounded agent execution. Phase 4B.3 carries those results through typed evidence, deterministic review, bounded context, and task-specific synthesis. Phase 4B.4 adds bounded, thread-scoped continuity. Phase 4C adds deterministic selection of at most one or two search results and bounded cross-source deepening for requests that actually ask for focal analysis.

No Product backend, frontend, public API, PostgreSQL schema, Qdrant schema, or Neo4j schema is changed. Phase 4B.4 bumped only the internal ThreadState JSON contract from 4 to 5 while retaining version 3/4 readers; Product continues storing the same bounded `stateJson`.

## Semantic contract

```text
Asset/IP                         conversational entity
role/vendor/product/status/...  structured selector
structured query result         Asset set
```

Structured semantic intents:

```text
asset_search
asset_aggregate
```

The Router may attach one validated `StructuredQuerySpec` using the Phase 4A allow-lists. The contract contains no Cypher, arbitrary property names, regex, OR expression, traversal instruction, or free-form operator.

For set queries:

```text
scope = none
direction = none
depth = 0
requires_graph = true
entity_binding = none
requires_multiple_entities = false
```

Profile and Detection remain off during semantic set routing because no focal Asset exists yet. Phase 4C may later select at most one or two returned Assets deterministically and deepen only those.

The Router extension now also accepts its own prompt-compliant `structured_result_reference: null` field on ordinary routes. The extension field is parsed and removed before the legacy/set allow-list validator runs, so normal, structured, and repair routes share one consistent contract.

## Phase 4B.2 execution contract

```text
asset_search / asset_aggregate
→ StructuredQuerySpec
→ TaskSpec.structured_query
→ deterministic Direct Plan
→ PlanValidator
→ Graph Specialist
→ CapabilityExecutor
→ graph.search_assets / graph.aggregate_assets
→ GraphService
→ Neo4jGraphRepository active projection
```

Both structured capabilities are read-only, planner-visible, depth zero, and require exactly zero focal entities. Planner-safe schemas expose only bounded typed fields:

```text
search:    filters, sort, direction, limit
aggregate: filters, operation, group_by, limit
```

Cursor replay remains service-internal. One Asset-set retrieval remains one capability call regardless of matched row count; returned rows never enter `TaskSpec.entities`, active entities, or the two-entity budget.

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

Search and aggregate use distinct evidence classes. Query identity is a canonical SHA-256 fingerprint of normalized query semantics plus active graph version. The Reviewer validates mode, query identity, graph version, provenance, counts/groups, and truncation locally. Valid empty results remain valid evidence; truncated results remain answerable with limitations.

Context composition distinguishes retrieval bounds from model-context bounds. Search context includes only a bounded subset of rows; aggregate context preserves total semantics even when groups are bounded. The Synthesizer keeps Neo4j organizational projection separate from Product current/deep truth.

## Phase 4B.4 structured continuity contract

```text
reviewed structured ToolResult
→ update_memory
→ bounded StructuredQueryContext
→ SessionRoutingState / ThreadMemoryState v5
→ owner-scoped ThreadStateStore
→ next request Router summary
→ typed semantic reference
→ deterministic materialization
```

`StructuredQueryContext` retains canonical query semantics and identity, active graph version, counts/truncation, source timestamps, and at most eight ordered identity-only search refs or aggregate groups. It is continuity metadata, not current operational evidence, LTM, a WorkingFact, a baseline, or a Qdrant document.

Set-level follow-ups rerun a typed current query. Entity-selection follow-ups may materialize only one or two retained search refs into the established focal workflow. Historical recall remains explicitly non-current. Explicit message entities remain authoritative over structured-result selection.

## Phase 4C exact-search finalization

Eligible analytical searches now use a bounded two-stage flow:

```text
Exact Graph Search
→ bounded Asset-set evidence
→ deterministic max 1–2 candidate selection
→ Product Profile / Detection for selected Assets only
→ bounded Graph topology when required
→ optional Knowledge when explicitly useful
→ unified EvidencePack
→ deterministic review
→ bounded context
→ grounded Synth response
→ StructuredQueryContext continuity
```

Automatic deepening occurs only when the request asks for analysis/investigation and focal selection is deterministic:

- zero results: no deepening;
- one result: safe single-focal deepening;
- exactly two results plus comparison intent: bounded pair deepening;
- more than two results: requires explicit first/top/ranked-one or ranked-two semantics;
- highest/lowest-style ranking requires an explicit structured sort;
- ambiguity fails closed and remains a set-level answer.

Returned rows themselves never become active conversational entities. Phase 4C focal selection is execution-local. Existing explicit/UI/active authority remains unchanged, and explicit IPs continue to outrank structured-result references under the 4B.4 rules.

### Fan-out and call budget

There is no `N search rows → N Product calls` path. Product capabilities retain one-entity-per-call semantics and Phase 4C selects at most two focal Assets.

The existing global capability-call budget remains authoritative across both stages. The Stage-1 search call is subtracted before focal deepening. Optional Knowledge is removed first if needed; if required deepening still cannot fit, the deepening attempt fails closed while the structured search result remains available.

### Evidence and reviewer semantics

Stage-1 discovery and Stage-2 Product/Detection/Graph/Knowledge results remain separate first-class ToolResults and merge only in EvidencePack. Product/Detection failures on selected focal Assets produce material limitations. Neo4j projection is never treated as a substitute for missing Product truth. Optional Knowledge failure is a background-evidence caveat rather than proof about the environment.

The Synthesizer `asset_search` module now distinguishes **discovery result** from **verified focal-Asset analysis** and applies deeper Product/Detection/topology facts only to the selected focal Assets.

### Memory boundary

`StructuredQueryContext` remains the result-set continuity authority. Automatic Phase 4C focal selection does not replace the active entity/pair. Structured-search turns are excluded from focal investigation-baseline capture so selected focal evidence cannot be written under a zero-entity search baseline. Existing Working Memory, episode, LTM, and ThreadState schemas are unchanged.

## Exact-search property boundary

Structured Asset search is intentionally allow-listed rather than a generic property-query API.

Supported semantic selectors include exact `ip`, `asset_name`, `status`, `suggested_type`, `role`, `roles` membership, `vendor`, `product`, `tag`, `sub_tag`, and `enrichment_status`; numeric confidence/unknown-score ranges; and a `last_detection_at` range. Fixed sort/group allow-lists remain part of the typed contract.

Rows also return classification/enrichment metadata needed for discovery context. Internal active `graph_version` authority and scheduler metadata are not exposed as arbitrary user filters. This is intentional: “returned node property” does not automatically mean “safe semantic selector.” No raw-property or Cypher escape hatch is introduced.

## Prompt policy

Prompt changes remain intentionally small:

- Router already distinguishes Asset selectors from focal entities and keeps Product/Detection off until later focal selection; no extra Router verbosity was needed beyond the null-contract validator fix.
- Structured set tasks still use deterministic direct planning; the Planner does not own post-search candidate selection and no new Planner behavior/model call was added.
- Synth `asset_search` instructions now distinguish set discovery from verified selected-Asset analysis.
- The static Synth system core already contains Product/Graph/Knowledge/memory authority and temporal rules and was not enlarged.

## Observability

Phase 4C logs bounded operational metadata for candidate count, selected count, selection reason, search truncation, Product call count, topology call count, Knowledge call count, partial focal failures, and focal evidence-review limitations. It does not log raw protected payloads or secrets.

## Phase 4A.1 measurement gate

`app/scripts/audit_phase4a1_query_plans.py` is read-only. It inspects the published graph with `EXPLAIN` plus optional `PROFILE` for representative structured queries. It never bootstraps schema, publishes snapshots, writes Assets, or creates indexes.

```bash
PYTHONPATH=app SOORIN_PHASE4A1_PROFILE=1 \
.venv/bin/python app/scripts/audit_phase4a1_query_plans.py
```

Index changes remain measurement-driven. `matched_total` and `/graph/stats` full-IP materialization remain separate scale items rather than semantic-routing changes.

## CI gate

`.github/workflows/phase4-validation.yml` uses a disposable Neo4j Community service on `dev` pushes and now runs:

1. compileall;
2. Phase 4A–4C routing/contracts/regressions, including Router null-reference and candidate/fan-out guards;
3. structured Neo4j integration regression;
4. read-only EXPLAIN/PROFILE audit;
5. Neo4j Community regression.

Mutating graph tests must never target the main runtime database.

## Current status

```text
Phase 4A      DONE
Phase 4A.1    DONE / VALIDATED
Phase 4B.1    DONE
Phase 4B.2    DONE
Phase 4B.3    DONE
Phase 4B.4    DONE
Phase 4C      IMPLEMENTED; final CI gate required on current HEAD
```

Detailed Phase 4C design and production boundaries are recorded in `PHASE4C_EXACT_SEARCH_FINALIZATION.md`.