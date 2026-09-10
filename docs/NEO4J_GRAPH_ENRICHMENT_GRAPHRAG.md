# Neo4j Graph Enrichment and GraphRAG

Status: living architecture record. Update this document whenever graph schema, topology synchronization, enrichment, scheduling, structured graph retrieval, graph capabilities, GraphRAG retrieval, or Copilot evidence integration changes.

Last synchronized: 2026-09-10.

```text
Phase 1       DONE
Phase 2       DONE
Phase 2.5     PASS
Phase 3       DONE
Phase 3.5     DONE
Phase 4A      IMPLEMENTED
Phase 4A.1    HARDENING / PRE-4B
Phase 4B      NEXT
```

## 1. Purpose and authority

Neo4j is Soorin Copilot's operational organizational graph and the structural substrate for GraphRAG. Product remains authoritative for current/deep Asset and Detection facts and for the topology source. Neo4j contains a versioned, query-bounded projection used for organizational discovery, exact structured lookup, topology analysis, and later hybrid GraphRAG. Qdrant continues to own semantic cybersecurity documentation and the derivative memory semantic index.

Authority boundaries:

```text
Neo4j             organizational Asset discovery + topology projection
Product API       current/deep Asset Profile and Detection authority
Qdrant Knowledge semantic cybersecurity documentation
Memory            historical continuity, baselines, durable findings
```

Neo4j enrichment is a discovery projection. It must never be presented as equivalent to a fresh Product Profile/Detection response.

## 2. Scale targets and non-negotiable bounds

Target scale remains at least 1,000,000 `Asset` nodes, 10,000,000 `COMMUNICATES_WITH` relationships, and 15+ Product-derived properties per Asset.

Runtime design therefore requires:

- bounded memory and bounded model context;
- active-version-only reads;
- keyset pagination instead of large `SKIP`/offset scans;
- parameterized Cypher values;
- bounded batch writes;
- controlled Product concurrency;
- last-known-good topology behavior;
- no full-graph hydration for ordinary requests;
- no one-Product-call-per-search-result fan-out;
- indexes added only after `EXPLAIN/PROFILE` evidence.

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

A staging version is unreadable through normal active-graph queries until publication succeeds. Fetch/write/validation failure preserves the previously published graph. Topology refresh is independently scheduled from enrichment.

Relevant implementation:

- `app/src/core/graph/refresh.py`
- `app/src/core/graph/neo4j.py`
- `app/src/core/graph/storage.py`

Phase 4A.1 hardening additionally ensures that failure telemetry cannot mask the original refresh failure if Neo4j becomes unavailable while the error is being reported, and raw snapshot temporary files are removed when serialization/write/replace fails.

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

Enrichment states:

| State | Meaning |
|---|---|
| `pending` | no successful enrichment yet |
| `success` | validated current projection persisted |
| `stale` | later refresh failed but prior successful values are retained |
| `error` | failed before any successful enrichment |
| `unavailable` | Product returned no usable overview before any successful enrichment |

Failure does not erase previously valid enrichment properties.

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

Matching Assets copy enrichment from the active version into the next staging topology before publication. New Assets start `pending`. This separates the approximately hourly topology cadence from the longer enrichment freshness cadence.

Topology publication and enrichment writes share the process-local graph mutation lock. Cross-process Product request serialization is protected by the renewable Neo4j lease.

## 7. Current stable graph capabilities

Current planner-visible/runtime graph capabilities remain:

```text
graph.get_summary
graph.get_neighbors
graph.get_relationship
graph.compare_assets
graph.find_path
```

They operate on already resolved focal Assets/IPs and provide topology evidence. No existing capability is removed in Phase 4A/4A.1.

## 8. Phase 4A structured Asset retrieval

Phase 4A is implemented at the graph data/service layer and is intentionally not agent-facing yet.

Implemented typed contracts:

```text
AssetSearchFilters
AssetSearchRequest
StructuredAssetRow
AssetSearchResult
AssetAggregateRequest
AssetAggregateGroup
AssetAggregateResult
```

Supported exact selectors:

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

Supported bounded ranges:

```text
model_confidence min/max
mapping_confidence min/max
unknown_score min/max
last_detection_at from/to
```

Supported aggregation:

```text
count
group_count
```

Supported sort fields are fixed enums; callers cannot supply arbitrary property names or Cypher.

Current limits:

```text
SOORIN_GRAPH_ASSET_SEARCH_DEFAULT_LIMIT=50
SOORIN_GRAPH_ASSET_SEARCH_MAX_LIMIT=200
```

These limits bound repository results. They are not model-context row budgets and they do not change `SOORIN_AGENT_MAX_ENTITIES=2`.

## 9. Entity vs selector semantics

This rule is authoritative for Phase 4B design:

```text
Asset/IP                         = conversational entity
role/vendor/product/status/...  = structured selector/filter/facet
search result                    = Asset set
```

A result set of 50 Assets does not create 50 active entities. Only Assets explicitly selected for further analysis become focal entities, still bounded by the existing two-entity conversational limit.

## 10. Phase 4A.1 hardening

Phase 4A.1 intentionally changes no Product backend, frontend, public API, PostgreSQL schema, Qdrant schema, Router, Planner, capability registry, EvidencePack, ContextComposer or memory schema.

### 10.1 Cursor safety

The external `GraphService.search_assets()` cursor is bound to:

- normalized filters;
- sort field;
- sort direction;
- the active graph version that produced the previous page.

The repository's keyset cursor is internal implementation detail. Replaying an external cursor with different selectors or after active topology publication changes must fail closed.

Changing only page size is allowed because it does not change query identity.

### 10.2 `matched_total`

`search_assets()` currently executes a separate `count(a)` query to return `matched_total`. This contract is kept unchanged in 4A.1. Before Phase 4B exposure, representative broad and selective queries must be measured with `PROFILE`; only measured evidence should justify making total-count optional or changing its implementation.

### 10.3 Index policy

Current schema bootstrap intentionally does not create one index per enriched property. Existing relevant indexes include active-version/IP and enrichment-scheduling support. Role/product/vendor/status/confidence indexes are not added merely because Phase 4A can filter those fields.

Before 4B, run `EXPLAIN/PROFILE` for at least:

```text
exact IP
role
product
vendor
status
role + product + status
model_confidence range
unknown_score range
enrichment_status
roles[] membership
count
group_count
```

Add only the smallest indexes whose plans demonstrate material benefit on representative graph scale.

`roles[]` membership remains a known scan-sensitive shape unless the future schema changes its representation.

### 10.4 Known scale audit item outside the Phase 4A result contract

`Neo4jGraphRepository.stats()` currently derives IP-range distribution from all active IPs. This is a separate `/graph/stats` performance path, not Phase 4A structured retrieval. It must be profiled/refactored before claiming 1M-node operational efficiency, but 4A.1 does not alter its public result contract without measured Neo4j evidence.

## 11. Current Phase 4A limitations

- structured search/aggregate methods are not registered agent capabilities yet;
- Router/TaskSpec do not yet carry first-class structured Asset-set semantics;
- EvidenceRequirementPolicy/Reviewer/ContextComposer do not yet have set-search evidence classes;
- result-set conversational continuity (`those assets`, `the first two`) is not yet implemented;
- no public `/graph/search` endpoint exists;
- no Product fan-out from a structured result set is implemented;
- no full-text/vector/hybrid Asset retrieval exists yet.

These are Phase 4B+ work, not defects to solve by bypassing current bounded agent contracts.

## 12. Phase 4B target

Phase 4B should integrate exact structured retrieval into the bounded agent workflow in small patches:

```text
1. typed structured-query semantic contract
2. Router/TaskSpec representation
3. graph.search_assets + graph.aggregate_assets capabilities
4. direct-plan + Planner catalog support
5. set-aware evidence classes and Reviewer behavior
6. bounded model-context projection
7. short result-set continuity without changing focal entity semantics
```

No fourth LLM role is required by default. Simple set queries should remain Router + deterministic validation/direct planning + Synthesizer; Planner remains for genuine multi-step tasks.

## 13. Near-future GraphRAG roadmap

```text
Phase 4B  agent integration for exact structured retrieval
Phase 5   measured Neo4j full-text retrieval
Phase 6   semantic Asset search with controlled Asset text + vector index
Phase 7   hybrid exact/full-text/vector seed selection + bounded traversal
Phase 8   Organizational RAG across Neo4j + Product + Qdrant + Memory
Phase 9   expensive/offline graph intelligence and temporal analytics
```

Target retrieval flow:

```text
User
→ TurnPolicy / entity + selector resolution
→ Router
→ direct plan or bounded Planner
→ Neo4j organizational candidate retrieval
→ bounded candidate selection
→ selective Product Profile/Detection for at most the needed focal Assets
→ optional bounded topology expansion
→ optional Qdrant security knowledge
→ EvidencePack
→ ContextComposer
→ Synthesizer
→ bounded memory/result-set continuity update
```

## 14. Frontend/backend/database boundary

Phase 4A/4A.1 changes no external contract.

Current chat request fields remain:

```text
conversation_id
session_id
request_id
message
ui_context.selected_ip
```

Product-backed Streamlit and legacy/local Streamlit continue using `/chat` or `/chat/stream`. Existing graph routes remain unchanged. Product/PostgreSQL continues to own production chat/memory persistence where configured; Qdrant remains semantic knowledge/memory indexing; Neo4j remains graph projection/storage.

A future Asset-set UI may render bounded rows/filter chips and allow selecting an Asset into `selected_ip`, but that is intentionally deferred until agent/backend contracts are stable.

## 15. Validation history and next acceptance gate

Completed before 4A.1:

- Phase 3/3.5 focused offline suites passed;
- isolated Neo4j Community regression suite passed (`14 passed` in the latest local run supplied for 4A);
- Phase 4A structured retrieval suite passed (`9 passed`, one Neo4j driver preview warning in the latest supplied local run);
- `git diff --check` was clean before the Phase 4A commit;
- Phase 4A commit: `65c5a4d` (`feat(graph): add structured asset retrieval foundation`).

After pulling Phase 4A.1 locally, run:

```bash
.venv/bin/python -m pytest \
  app/src/tests/test_structured_graph_retrieval.py \
  app/src/tests/test_graph_phase4a1_hardening.py -q

SOORIN_RUN_NEO4J_INTEGRATION=1 \
SOORIN_NEO4J_URI=bolt://127.0.0.1:<isolated-port> \
SOORIN_NEO4J_USER=neo4j \
SOORIN_NEO4J_PASSWORD='<isolated-password>' \
.venv/bin/python -m pytest \
  app/src/tests/test_structured_graph_neo4j_integration.py \
  app/src/tests/test_neo4j_community_parity.py -q

.venv/bin/python -m compileall -q app/src
git diff --check
```

Never run the mutating integration suites against the main Neo4j runtime.

The GitHub-side Phase 4A.1 commit can be statically reviewed and committed here, but local Docker/Neo4j execution remains the final runtime acceptance evidence before promoting the same commit to the organization repository.
