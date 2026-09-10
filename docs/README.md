# Documentation map and current authority

Last synchronized: 2026-09-11 (`dev`, Phase 4B.2 capability integration).

This directory contains both living specifications and historical audit records. Historical audit files are intentionally retained as snapshots of the repository state at the date written; they are not silently rewritten to look current. For implementation decisions, source code on `dev` is authoritative, followed by the living/current documents below.

## Current / living documents

| Document | Status | Current use |
|---|---|---|
| `CURRENT_ARCHITECTURE.md` | Living architecture baseline with dated addenda | Overall bounded LangGraph, LLM roles, evidence, context, memory, Product, Neo4j and Qdrant architecture. Read together with the current Phase 4 records below. |
| `NEO4J_GRAPH_ENRICHMENT_GRAPHRAG.md` | Living graph architecture record | Authoritative graph/enrichment/structured-search/GraphRAG roadmap. |
| `PHASE4_STRUCTURED_GRAPH_ROUTING.md` | Current Phase 4A.1/4B.2 implementation record | Exact-search hardening, Router semantic Asset-set contract, and bounded capability execution. |
| `ENVIRONMENT_VARIABLES.md` | Living configuration reference | Current settings and deployment knobs. Phase 4A search limits are documented here. |
| `DEPLOYMENT.md` | Current deployment guidance | Runtime/deployment mechanics. No Phase 4B.1 deployment-contract change. |
| `OBSERVABILITY.md` | Current observability guidance | Metrics/logging boundaries. No Phase 4B.1 metric contract change. |
| `FRONTEND_BACKEND_COPILOT_INTEGRATION.md` | Integration contract plus historical baseline sections | Product/Streamlit request identity, chat, persistence and ownership boundaries. Dated Git baselines are historical; current `dev` source supersedes old commit hashes. |

## Historical / design-audit documents

| Document | Status | How to read it |
|---|---|---|
| `INTERNAL_EVIDENCE_TO_MODEL_CONTEXT_AUDIT.md` | Historical audit with later addenda | Useful evidence-flow history. Current source and `CURRENT_ARCHITECTURE.md` supersede old provider/context claims. |
| `MEMORY_CONTEXT_UPGRADE_DESIGN.md` | Historical design record | Design rationale. Current memory behavior is described by source and the current-dev audit. |
| `MEMORY_WORKFLOW_CURRENT_DEV_AUDIT.md` | Dated audit with later implementation addenda | Detailed memory/control-plane history. Treat old HEAD/status tables as historical. |
| `agentic-foundation-rag.md` | Historical foundation/design record | Early agentic/RAG architecture rationale. Current bounded workflow and GraphRAG roadmap supersede it where they differ. |

## Current Phase 4 status

```text
Phase 4A      IMPLEMENTED
Phase 4A.1    CODE HARDENED; isolated CI/profile validation added
Phase 4B.1    SEMANTIC CONTRACT IMPLEMENTED
Phase 4B.2    IMPLEMENTED: capability/execution integration
```

Phase 4A contains typed exact/range Asset filters, count/group-count aggregation, active-version-only Neo4j queries, bounded keyset pagination and fixed sort/group allow-lists. Phase 4A.1 binds the service cursor to normalized query identity and active graph version, hardens refresh/storage failure paths, and provides a read-only `EXPLAIN/PROFILE` audit plus isolated CI Neo4j validation.

Phase 4B.1 adds a typed `StructuredQuerySpec`, Router semantic intents `asset_search` and `asset_aggregate`, deterministic validation, and structured-query fields in semantic/task contracts. Phase 4B.2 registers planner-visible, read-only `graph.search_assets` and `graph.aggregate_assets` capabilities with zero focal-entity cardinality. Simple set queries compile deterministically into one validated Graph Specialist call through `GraphService` to the active Neo4j projection. Cursor replay remains internal, configured result limits remain authoritative, and no Product fan-out or new LLM is added.

The semantic rule remains fixed:

```text
Asset/IP                         = conversational entity
role/vendor/product/status/...  = structured selector/filter/facet
structured retrieval output     = Asset set
```

Asset-set rows do not consume the two-focal-entity budget. A later bounded result-set continuity object may reference a set, while only selected Assets become focal entities for Product/Detection/topology deepening.

Phase 4B.3 remains responsible for Asset-set-aware EvidencePack, Reviewer, ContextComposer, citation, and Synthesizer behavior. Phase 4B.4 remains responsible for short-term result-set continuity, and Phase 4C for selective cross-source deepening.

## Authority boundaries for GraphRAG

```text
Neo4j             organizational discovery, enriched Asset projection, topology
Product API       current/deep operational Asset and Detection authority
Qdrant Knowledge semantic cybersecurity documentation
Memory            historical continuity, validated baselines and durable findings
```

Neo4j enrichment is a discovery projection. It must not be represented as equivalent to a fresh Product Profile/Detection response.

## Validation policy

The mutating Neo4j integration suites must run only against a disposable database. They must never target the main `soorin-copilot-neo4j` instance.

The repository now includes `.github/workflows/phase4-validation.yml`, which uses a disposable Neo4j Community service on `dev` pushes to run Phase 4 offline tests, structured integration coverage, the read-only query-plan audit, and the existing Community parity regression.

For production-representative read-only planning evidence, `app/scripts/audit_phase4a1_query_plans.py` may be run against the configured real Neo4j instance with `SOORIN_PHASE4A1_PROFILE=1`. It performs no writes and creates no indexes.

Index policy remains measurement-driven. No enriched-property index should be added merely because a filter exists. `matched_total` and the existing `/graph/stats` full-IP materialization remain explicit performance items to measure before changing their contracts or implementations.
