# Documentation map and current authority

Last synchronized: 2026-09-10 (`dev`, Phase 4A.1 preparation).

This directory contains both living specifications and historical audit records. Historical audit files are intentionally retained as snapshots of the repository state at the date written; they are not silently rewritten to look current. For implementation decisions, source code on `dev` is authoritative, followed by the living/current documents below.

## Current / living documents

| Document | Status | Current use |
|---|---|---|
| `CURRENT_ARCHITECTURE.md` | Living architecture baseline with dated addenda | Overall bounded LangGraph, LLM roles, evidence, context, memory, Product, Neo4j and Qdrant architecture. Read together with the Phase 4 addendum below. |
| `NEO4J_GRAPH_ENRICHMENT_GRAPHRAG.md` | Living graph architecture record | Authoritative graph/enrichment/structured-search/GraphRAG roadmap. Phase 4A is implemented; Phase 4A.1 hardening precedes agent integration. |
| `ENVIRONMENT_VARIABLES.md` | Living configuration reference | Current settings and deployment knobs. Phase 4A search limits are documented here. |
| `DEPLOYMENT.md` | Current deployment guidance | Runtime/deployment mechanics. No Phase 4A.1 deployment-contract change. |
| `OBSERVABILITY.md` | Current observability guidance | Metrics/logging boundaries. No Phase 4A.1 metric contract change. |
| `FRONTEND_BACKEND_COPILOT_INTEGRATION.md` | Integration contract plus historical baseline sections | Product/Streamlit request identity, chat, persistence and ownership boundaries. The dated Git baselines inside are historical; current `dev` source supersedes old commit hashes. |

## Historical / design-audit documents

| Document | Status | How to read it |
|---|---|---|
| `INTERNAL_EVIDENCE_TO_MODEL_CONTEXT_AUDIT.md` | Historical audit with later addenda | Useful evidence-flow history. Current source and `CURRENT_ARCHITECTURE.md` supersede old provider/context claims. |
| `MEMORY_CONTEXT_UPGRADE_DESIGN.md` | Historical design record | Design rationale. Current memory behavior is described by source and the current-dev audit. |
| `MEMORY_WORKFLOW_CURRENT_DEV_AUDIT.md` | Dated audit with later implementation addenda | Detailed memory/control-plane history. Treat old HEAD/status tables as historical. |
| `agentic-foundation-rag.md` | Historical foundation/design record | Early agentic/RAG architecture rationale. Current bounded workflow and GraphRAG roadmap supersede it where they differ. |

## Current Phase 4 architecture addendum

The graph project has advanced beyond the older `Phase 4 NEXT` status that remains in some dated audit prose. Current source on `dev` contains Phase 4A's backend structured retrieval foundation:

- typed `AssetSearchFilters`, `AssetSearchRequest`, `AssetSearchResult`;
- typed count/group-count aggregation contracts;
- allow-listed exact/range selectors;
- active-graph-version-only Neo4j queries;
- bounded keyset pagination and safe sort fields;
- parameterized Cypher values;
- service/repository boundaries and isolated Neo4j integration coverage.

Phase 4A does **not** yet change Copilot agent behavior. `graph.search_assets` and `graph.aggregate_assets` are not registered capabilities yet; Router, Planner, TaskSpec, EvidencePack, ContextComposer, memory schema, Product frontend, Streamlit request contract, public graph API, Product backend, PostgreSQL and Qdrant remain unchanged.

The semantic rule for the next phases is fixed:

```text
Asset/IP                         = conversational entity
role/vendor/product/status/...  = structured selector/filter/facet
structured retrieval output     = Asset set
```

Asset-set rows must not consume the two-focal-entity budget. A future result-set continuity object may refer to a bounded Asset set, while only selected Assets become focal entities for Product/Detection/topology deepening.

## Authority boundaries for GraphRAG

```text
Neo4j            organizational discovery, enriched Asset projection, topology
Product API      current/deep operational Asset and Detection authority
Qdrant Knowledge semantic cybersecurity documentation
Memory           historical continuity, validated baselines and durable findings
```

Neo4j enrichment is a discovery projection. It must not be represented as equivalent to a fresh Product Profile/Detection response.

## Phase 4A.1 acceptance boundary

Before Phase 4B agent integration:

1. structured pagination cursors must be bound to normalized filters, sort/direction and the active graph version;
2. Phase 4A tests must cover cursor replay rejection and graph-version changes;
3. refresh/storage failure paths must preserve the last-known-good graph and avoid masking the original failure;
4. representative structured queries must be measured locally with Neo4j `EXPLAIN/PROFILE` before adding indexes;
5. the unconditional `matched_total` count query must be measured before changing its contract;
6. no Router/Planner/Capability Registry/EvidencePack/ContextComposer/Memory/frontend/Product/PostgreSQL/Qdrant/public-API behavior is changed in 4A.1.

### Required local validation after pulling the 4A.1 commit

Run the normal offline graph tests plus the isolated Neo4j Community suites. The mutating integration database must be disposable and must not be the main `soorin-copilot-neo4j` instance.

At minimum:

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

The GitHub connector can inspect and commit source, but it cannot execute your local Docker/Neo4j runtime; those local integration results remain the final acceptance evidence.
