# Neo4j Graph Enrichment and GraphRAG

Status: living architecture record. Update whenever graph schema, topology synchronization, enrichment, scheduling, structured retrieval, graph capabilities, GraphRAG retrieval, or Copilot evidence integration changes.

Last synchronized: 2026-09-11.

```text
Phase 1       DONE
Phase 2       DONE
Phase 2.5     PASS
Phase 3       DONE
Phase 3.5     DONE
Phase 4A      IMPLEMENTED
Phase 4A.1    CODE COMPLETE; CI/PROFILE VALIDATION GATE
Phase 4B.1    IMPLEMENTED
Phase 4B.2    IMPLEMENTED
Phase 4B.3    IMPLEMENTED
Phase 4B.4    IMPLEMENTED
```

## 1. Purpose and authority

Neo4j is Soorin Copilot's operational organizational graph and the structural substrate for GraphRAG. Product remains authoritative for current/deep Asset and Detection facts and for topology source data. Neo4j contains a versioned, query-bounded projection for organizational discovery, exact structured lookup, topology analysis, and later hybrid GraphRAG. Qdrant continues to own semantic cybersecurity documentation and the derivative memory semantic index.

```text
Neo4j             organizational Asset discovery + topology projection
Product API       current/deep Asset Profile and Detection authority
Qdrant Knowledge semantic cybersecurity documentation
Memory            historical continuity, baselines, durable findings
```

Neo4j enrichment is a discovery projection. It is not equivalent to a fresh Product Profile or Detection response.

## 2. Scale targets and non-negotiable bounds

Target scale remains at least 1,000,000 `Asset` nodes, 10,000,000 `COMMUNICATES_WITH` relationships, and 15+ Product-derived properties per Asset.

Required properties of the design:

- bounded memory and bounded model context;
- active-version-only reads;
- keyset pagination instead of large offset scans;
- parameterized Cypher values;
- bounded batch writes;
- controlled Product concurrency;
- last-known-good topology behavior;
- no full-graph hydration for ordinary requests;
- no one-Product-call-per-search-result fan-out;
- indexes added only after measured `EXPLAIN/PROFILE` evidence.

## 3. Topology plane

```text
Product GET /zeek/connections/unique-ip-pairs
→ GraphRefreshService
→ normalize + validate
→ versioned staging Assets/edges
→ validate candidate counts
→ publish GraphMetadata.active_graph_version
→ retire inactive projection
```

A staging version is not readable through normal active-graph queries until publication succeeds. Fetch/write/validation failure preserves the previously published graph. Topology refresh is independently scheduled from enrichment.

Relevant implementation:

- `app/src/core/graph/refresh.py`
- `app/src/core/graph/neo4j.py`
- `app/src/core/graph/storage.py`

Phase 4A.1 also ensures refresh-failure telemetry cannot mask the original failure if Neo4j is unavailable during failure reporting, and failed raw-snapshot writes remove temporary files.

## 4. Enrichment plane

```text
FastAPI startup
→ GraphEnrichmentRuntimeService
→ graph_enrichment_scheduler renewable Neo4j lease
→ bounded due-page selection from active Assets
→ AssetEnrichmentService
→ process-local Product lock
→ graph_enrichment_product_request renewable Neo4j lease
→ Product GET /asset-detection/{ip}/overview
→ ProductAssetDetectionOverview
→ AssetEnrichmentMutation
→ parameterized Neo4j batch update
```

Product overview concurrency remains exactly `1` across scheduled/manual/on-demand enrichment. Product HTTP is never performed inside a Neo4j transaction. Scheduler and Product-request leases are separate.

Enrichment states are `pending`, `success`, `stale`, `error`, and `unavailable`. Failure does not erase previously valid enrichment properties.

## 5. Product → Neo4j enrichment mapping

| Product field | Neo4j Asset property |
|---|---|
| `assetName` | `asset_name` |
| `ip` | `ip` |
| `status` | `status` |
| `suggestedType` | `suggested_type` |
| `modelConfidence` | `model_confidence` |
| `mappingConfidence` | `mapping_confidence` |
| `unknownScore` | `unknown_score` |
| `classificationSummary` | `classification_summary` |
| `vendor` | `vendor` |
| `product` | `product` |
| `role` | `role` |
| `roles` | `roles[]` |
| `tag` | `tag` |
| `subTag` | `sub_tag` |
| `lastDetectionAt` | `last_detection_at` |

Operational metadata also includes enrichment status, timestamps, source, version/hash, next-due time and error state.

## 6. Topology/enrichment version interaction

Matching Assets copy enrichment from the active version into the next staging topology before publication. New Assets start `pending`. This separates topology cadence from the longer enrichment freshness cadence.

Topology publication and enrichment writes share the process-local graph mutation lock. Cross-process Product request serialization is protected by renewable Neo4j leases.

## 7. Stable graph capabilities

Current runtime graph capabilities remain:

```text
graph.get_summary
graph.get_neighbors
graph.get_relationship
graph.compare_assets
graph.find_path
```

They operate on already resolved focal Assets/IPs and provide topology evidence. No existing capability is removed in Phase 4A/4B.1.

## 8. Phase 4A structured Asset retrieval

Phase 4A is implemented at the graph data/service layer. Implemented contracts include `AssetSearchFilters`, `AssetSearchRequest`, `AssetSearchResult`, `StructuredAssetRow`, `AssetAggregateRequest`, `AssetAggregateGroup`, and `AssetAggregateResult`.

Exact selectors:

```text
ip
asset_name
status
suggested_type
role
roles[] membership
vendor
product
tag
sub_tag
enrichment_status
```

Bounded ranges:

```text
model_confidence min/max
mapping_confidence min/max
unknown_score min/max
last_detection_at from/to
```

Aggregation supports `count` and `group_count`. Sort/group fields are fixed enums; callers cannot supply arbitrary property names or Cypher.

Current row bounds:

```text
SOORIN_GRAPH_ASSET_SEARCH_DEFAULT_LIMIT=50
SOORIN_GRAPH_ASSET_SEARCH_MAX_LIMIT=200
```

These are retrieval bounds, not model-context budgets and not conversational entity limits.

## 9. Entity vs selector semantics

This rule is authoritative:

```text
Asset/IP                         = conversational entity
role/vendor/product/status/...  = structured selector/filter/facet
structured retrieval result     = Asset set
```

A result set does not create active conversational entities. Only Assets selected for deeper analysis may become focal entities, still bounded by `SOORIN_AGENT_MAX_ENTITIES=2`.

## 10. Phase 4A.1 hardening and measurement

The service-level external search cursor is bound to normalized filters, sort field, sort direction, and the active graph version that created the page. Changing only page size is allowed. Reusing a cursor with different query identity or after active graph publication changes fails closed.

`search_assets()` currently performs a separate count query for `matched_total`. That contract remains unchanged until measured evidence demonstrates material cost.

`app/scripts/audit_phase4a1_query_plans.py` is a read-only measurement tool. It can run representative `EXPLAIN` and optional `PROFILE` plans for exact IP, role, product, vendor, status, confidence, enrichment status and `matched_total`. It performs no schema bootstrap, data mutation, index creation, or graph publication.

```bash
PYTHONPATH=app SOORIN_PHASE4A1_PROFILE=1 \
.venv/bin/python app/scripts/audit_phase4a1_query_plans.py
```

No enriched-property indexes have been added by Phase 4A/4A.1. Index changes remain evidence-driven.

Known scale debt: `Neo4jGraphRepository.stats()` still materializes active IPs to derive range distribution. That public stats path must be measured/refactored before claiming 1M-node efficiency, while preserving its external response contract.

Mutating Community integration suites remain disposable-database-only. They must never run against the main Neo4j runtime.

## 11. Phase 4B.1 semantic contract and Phase 4B.2 execution

Phase 4B.1 introduces the semantic representation. Phase 4B.2 registers and executes the corresponding bounded capabilities.

New typed contract:

```text
StructuredQuerySpec
  mode = search | aggregate
  filters = Phase 4A AssetSearchFilters
  search: optional sort/direction/limit
  aggregate: operation=count|group_count, optional group_by/limit
```

New Router intents:

```text
asset_search
asset_aggregate
```

For both intents:

```text
scope = none
direction = none
depth = 0
requires_graph = true
entity_binding = none
requires_multiple_entities = false
```

The Router prompt now explicitly distinguishes focal Asset identity from Asset-set selectors and forbids arbitrary properties, Cypher, regex, free-form operators, OR expressions and traversal instructions inside `structured_query`.

Deterministic Pydantic validation reuses the Phase 4A allow-lists. `IntentDecision` and `RouteDecision` carry the structured semantic object, and `TaskSpec.structured_query` is its single bridge into execution. Existing non-set routes continue through the established validator.

For simple requests, the direct compiler transforms the validated structured query into exactly one typed `graph.search_assets` or `graph.aggregate_assets` step. Both capabilities are read-only, planner-visible, depth zero, and have `(0, 0)` focal-entity cardinality. The same schemas appear dynamically in the bounded Planner catalog for future composition, exposing only `filters/sort/direction/limit` or `filters/operation/group_by/limit`; cursor replay and arbitrary query fields are excluded.

The PlanValidator applies schema, duplicate-call, call-budget, zero-entity, depth, and configured result-limit checks. The existing Graph Specialist and CapabilityExecutor pass typed requests to `GraphService`, which delegates to the active-version-scoped `Neo4jGraphRepository`. One retrieval is one capability call regardless of result count, and rows remain an Asset set rather than active/focal entities.

No new LLM role is introduced. The existing Router remains semantic classification authority; deterministic code owns schema and invariants. There is no Product fan-out, Qdrant call, or Memory mutation in this path.

## 12. CI validation

`.github/workflows/phase4-validation.yml` uses a disposable Neo4j Community service on pushes to `dev`. It performs:

```text
compileall
Phase 4 offline contract tests
structured Neo4j integration regression
read-only EXPLAIN/PROFILE audit
existing Neo4j Community parity regression
```

This validation path prevents developers from having to point mutating integration tests at their main local Neo4j instance.

## 13. External boundaries unchanged

Phase 4A/4A.1/4B.1/4B.2/4B.3/4B.4 do not change Product backend endpoints, Product PostgreSQL schema, Streamlit/Product frontend request contracts, public graph API routes, Qdrant collections, or Neo4j schema. Phase 4B.4 changes only the internal bounded ThreadState JSON version, using the existing Product/local `stateJson` persistence boundary.

Current chat identity fields remain `conversation_id`, `session_id`, `request_id`, `message`, and optional `ui_context.selected_ip`.

## 14. Next phases

Phase 4B.3 implements set-aware evidence classes, EvidencePack/Reviewer semantics, bounded model-context projection, and task-aware synthesis. Phase 4B.4 implements one latest thread-scoped `StructuredQueryContext`: canonical query identity and graph version, bounded counts/truncation, and up to eight ordered identity refs or aggregate groups with a deterministic snapshot fingerprint. It is referential metadata, never Graph evidence or LTM. The existing semantic Router classifies set reuse, one/two-result selection, or historical recall; deterministic code validates and materializes the decision. Set reuse reruns current Neo4j evidence, and selected refs enter only the existing focal workflow. Phase 4C owns automatic bounded candidate selection and cross-source deepening.

## 15. Near-future GraphRAG roadmap

```text
Phase 4B.2 capability/execution integration (implemented)
Phase 4B.3 evidence/context integration (implemented)
Phase 4B.4 bounded result-set continuity (implemented)
Phase 4C   cross-source exact search → selective Product/Detection/topology deepening
Phase 5    measured Neo4j full-text retrieval
Phase 6    semantic Asset search with controlled Asset text + vector index
Phase 7    hybrid exact/full-text/vector seeding + bounded traversal
Phase 8    Organizational RAG across Neo4j + Product + Qdrant + Memory
Phase 9    expensive/offline graph intelligence and temporal analytics
```

Target flow:

```text
User
→ TurnPolicy / entity authority
→ semantic Router + optional StructuredQuerySpec
→ deterministic task validation
→ direct plan or bounded Planner
→ Neo4j organizational candidate retrieval
→ bounded candidate selection
→ selective Product Profile/Detection for only needed focal Assets
→ optional bounded topology expansion
→ optional Qdrant security knowledge
→ EvidencePack
→ ContextComposer
→ Synthesizer
→ bounded memory/result-set continuity update
```
