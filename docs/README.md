# Documentation map and current authority

Last synchronized: 2026-09-11 (`dev`, Phase 4C exact-search finalization).

This directory contains living specifications and historical audit records. Historical audit files remain snapshots of the repository state at the date written; source on `dev` is authoritative, followed by the living/current documents below.

## Current / living documents

| Document | Status | Current use |
|---|---|---|
| `CURRENT_ARCHITECTURE.md` | Living architecture baseline with dated addenda | Overall bounded LangGraph, LLM roles, evidence, context, memory, Product, Neo4j and Qdrant architecture. Read with the current Phase 4 records. |
| `NEO4J_GRAPH_ENRICHMENT_GRAPHRAG.md` | Living graph architecture record | Graph/enrichment/structured-search/GraphRAG architecture and roadmap. |
| `PHASE4_STRUCTURED_GRAPH_ROUTING.md` | Phase 4A.1–4C implementation record | Exact-search hardening, Router/capability execution, Asset-set evidence, continuity, and bounded cross-source deepening. |
| `PHASE4C_EXACT_SEARCH_FINALIZATION.md` | Current Phase 4C validated implementation record | Bounded candidate selection, selective Product/Detection/topology deepening, reviewer/context/memory boundaries, observability and validation. |
| `ENVIRONMENT_VARIABLES.md` | Living configuration reference | Current settings and deployment knobs. |
| `DEPLOYMENT.md` | Current deployment guidance | Runtime/deployment mechanics. |
| `OBSERVABILITY.md` | Current observability guidance | Metrics/logging boundaries. |
| `FRONTEND_BACKEND_COPILOT_INTEGRATION.md` | Integration contract plus historical baseline sections | Product/Streamlit request identity, chat, persistence and ownership boundaries. Current `dev` source supersedes old commit hashes. |

## Historical / design-audit documents

| Document | Status | How to read it |
|---|---|---|
| `INTERNAL_EVIDENCE_TO_MODEL_CONTEXT_AUDIT.md` | Historical audit with later addenda | Useful evidence-flow history. Current source and architecture docs supersede old provider/context claims. |
| `MEMORY_CONTEXT_UPGRADE_DESIGN.md` | Historical design record | Design rationale. Current memory behavior is described by source and the current-dev audit. |
| `MEMORY_WORKFLOW_CURRENT_DEV_AUDIT.md` | Dated audit with later implementation addenda | Detailed memory/control-plane history. Treat old HEAD/status tables as historical. |
| `agentic-foundation-rag.md` | Historical foundation/design record | Early agentic/RAG rationale. Current bounded workflow and GraphRAG records supersede it where they differ. |

## Current Phase 4 status

```text
Phase 4A      DONE
Phase 4A.1    DONE / VALIDATED
Phase 4B.1    DONE
Phase 4B.2    DONE
Phase 4B.3    DONE
Phase 4B.4    DONE
Phase 4C      DONE / VALIDATED
```

Phase 4A provides typed exact/range Asset filters, count/group-count aggregation, active-version-only Neo4j queries, bounded keyset pagination and fixed sort/group allow-lists. Phase 4A.1 binds cursors to normalized query identity and active graph version, hardens refresh/storage failure paths, and provides read-only `EXPLAIN/PROFILE` plus isolated Neo4j validation.

Phase 4B.1 introduced `StructuredQuerySpec`, Router intents `asset_search`/`asset_aggregate`, deterministic validation, and selector-vs-entity semantics. Phase 4B.2 registered read-only zero-entity `graph.search_assets` and `graph.aggregate_assets` capabilities. Phase 4B.3 added typed Asset-set evidence, query identity, EvidencePack retention, deterministic review, bounded context serialization and Synth task modules. Phase 4B.4 added owner/thread-scoped `StructuredQueryContext` with bounded ordered refs and semantic follow-up handling.

The semantic rule remains fixed:

```text
Asset/IP                         = conversational entity
role/vendor/product/status/...  = structured selector/filter/facet
structured retrieval output     = Asset set
```

Phase 4C turns an eligible exact-search result into a bounded investigation entry point:

```text
Exact Search
→ bounded Asset-set discovery
→ deterministic selection of max 1–2 focal Assets
→ Product Profile / Detection for selected Assets only
→ bounded Graph topology when required
→ optional Knowledge when explicitly useful
→ unified EvidencePack
→ deterministic review
→ bounded context
→ grounded Synth response
→ StructuredQueryContext continuity
```

Automatic deepening is not universal. Plain list/search requests remain set-level answers. A one-result search can deepen when analysis was requested; multi-result search deepening requires deterministic selection semantics. Ambiguous multi-candidate requests fail closed. There is no `N results → N Product calls` path, and the existing six-call/global two-focal-entity bounds remain authoritative.

## Authority boundaries for GraphRAG

```text
Neo4j             organizational discovery, enriched Asset projection, topology
Product API       current/deep operational Asset and Detection authority
Qdrant Knowledge semantic cybersecurity documentation
Memory            historical continuity, validated baselines and durable findings
```

Neo4j enrichment is a discovery projection. It must not be represented as equivalent to a fresh Product Profile/Detection response. Phase 4C reviewer logic preserves this distinction when Product or Detection verification is missing or partial.

## Exact-search property boundary

Structured search exposes only fixed semantic selectors and range/sort/group allow-lists. Returned rows also contain enrichment/projection metadata. Internal graph-version authority and scheduler metadata are not turned into arbitrary user property filters. No raw property, Cypher, regex, OR-expression, or operator escape hatch exists.

## Memory boundary

Asset-set rows never consume the two-focal-entity budget and never become active entities merely because a search ran. `StructuredQueryContext` remains the bounded result-set continuity mechanism. Phase 4C auto-selected focal Assets are execution-local; structured-search turns do not capture a focal investigation baseline under an empty search context. Existing Working Memory/LTM schemas and explicit/active entity authority remain unchanged.

## Validation policy

Mutating Neo4j integration suites must run only against a disposable database, never the main `soorin-copilot-neo4j` instance.

`.github/workflows/phase4-validation.yml` uses a disposable Neo4j Community service on `dev` pushes and covers the Router null-reference contract, Phase 4C deterministic selection/fan-out/reviewer guards, prior Phase 4 regressions, structured Neo4j integration, read-only query-plan audit, and Community parity tests.

Final validated code commit `613dab022cb9e5f58690e0f193b034613f7cf010` passed `237` targeted tests (`41` skipped, `62` subtests), `5` structured Neo4j integration tests, and `14` Community parity tests. The query-plan audit reported `writes_performed=false`.

`app/scripts/audit_phase4a1_query_plans.py` remains read-only. Index policy remains measurement-driven; no enriched-property index is added merely because a filter exists. `matched_total` and the existing `/graph/stats` full-IP materialization remain explicit scale items to measure before changing contracts or implementations.
