# Soorin Agentic Foundation and SOC Knowledge RAG

## Baseline boundary

The existing entity resolver, semantic LLM router, deterministic route validation,
Profile/Detection/Neo4j graph providers, context budget controls, final model, streaming
events, and session-state updates remain the direct execution path. LangGraph now
provides a bounded workflow shell around that path. It does not introduce a planner,
tool loop, external action, or new public API.

## Reference migration classification

| Reference unit | Decision | Current treatment |
|---|---|---|
| `citation_formatter.py` | adapt | Stable source/chunk citations without ML Engine trust assumptions. |
| `chunker.py` | adapt | Deterministic character chunks with content-based UUID chunk IDs. |
| `document_loader.py` | redesign | Explicit external-root discovery/loading; no generated ML artifacts or import-time scanning. |
| `embedder.py` | do_not_migrate | The duplicate TF-IDF helper is not part of the dense Hugging Face/Qdrant path. |
| `embeddings/base.py` | adapt | Replaced by a small Soorin `Embedder` protocol. |
| legacy dense embedding implementation | adapt | Replaced by a model-neutral lazy Hugging Face embedder using mean pooling and normalized vectors. |
| legacy embedding worker | do_not_migrate | Platform/runtime subprocess policy is ML Engine-specific and remains outside this foundation. |
| `embeddings/tfidf.py` | do_not_migrate | No silent fallback to a semantically different or stale local index. |
| `backends/registry.py` | redesign | Replaced by typed vector-store health and explicit configured backend metadata. |
| `index/manifest.py` | redesign | Qdrant collection configuration and payload metadata replace local index manifests. |
| `index/vector_store.py` | do_not_migrate | NumPy/Pandas/FAISS storage is replaced by a Soorin-owned protocol and Qdrant adapter. |
| `index_builder.py` | redesign | Explicit operator-only corpus validation/indexing; no eager build or runtime fallback. |
| `retriever.py` | redesign | Qdrant search returns typed chunks; no direct index loading or eager query model loading. |
| `rag_service.py` | redesign | Small `knowledge.search` capability with typed unavailable/empty/partial states. |
| `safety.py` | reuse_now | Prompt-injection detection is retained in a smaller retrieval boundary. |
| `schema.py` | adapt | Replaced by Copilot-owned knowledge, capability, evidence, and review contracts. |
| `sources.py` | adapt | Stable external relative paths, hashes, categories, and source versions. |

## Qdrant architecture

`KnowledgeSearchService` depends on the Soorin `VectorStore` protocol, not the
Qdrant client. `QdrantVectorStore` validates collection dimension and distance,
supports metadata filters, scored retrieval, batched upserts, deletion, health,
and unavailable states. Payloads contain stable document/chunk identifiers,
relative/source paths, section, category, title, content hash, indexing time,
source version, and chunk text.

Qdrant is the configured default. FAISS is not retained because maintaining a
second persisted backend would add duplicate indexing logic and could silently
serve stale evidence. There is no automatic backend fallback.

## External corpus

Keep the SOC corpus outside the Copilot repository and configure or mount it with:

```text
SOORIN_RAG_SOURCE_ROOT=/path/to/soc-knowledge-base
```

Neither application import nor startup scans this path. The corpus is scanned only
by the explicit validation/index command. Missing corpus, embedding dependencies,
or Qdrant returns a typed RAG limitation and does not break unrelated routes.

## Workflow foundation

The bounded workflow stages are:

```text
resolve -> route -> validate_task -> select_workflow -> execute_direct
        -> build_evidence -> review_evidence -> synthesize
```

The current semantic router remains the normal decision source. Validated routes
map into `TaskSpec`. Direct requests do not invoke a planner. Composite requests
can be marked `multi_step`, but this release intentionally executes the existing
bounded direct pipeline and exposes only a future planner extension point.
Evidence Reviewer V1 is deterministic and distinguishes sufficient, limited,
missing-required-evidence, and safe-failure outcomes.

Future Neo4j support should implement the existing graph capability boundary; it
must not change entity authority, route semantics, or allow unrestricted Cypher.

## Operator-only indexing sequence

Do not run these commands as part of application startup.

1. Start the optional persistent Qdrant profile:

   ```bash
   docker compose --profile rag up -d qdrant
   ```

2. Configure the repository-root `.env` with the external source root, Qdrant URL, collection,
   embedding model/dimension, and `SOORIN_RAG_ENABLED=true`. Inside the Compose
   network use `SOORIN_RAG_QDRANT_URL=http://qdrant:6333`.

3. Validate the corpus without generating embeddings or writing Qdrant:

   ```bash
   PYTHONPATH=app .venv/bin/python -m src.core.rag.indexer
   ```

4. Explicitly build embeddings and create/update the collection:

   ```bash
   PYTHONPATH=app .venv/bin/python -m src.core.rag.indexer --build
   ```

5. Inspect collection statistics using the local Qdrant API or dashboard:

   ```bash
   curl -s http://127.0.0.1:6333/collections/soorin_soc_knowledge_bge_base_v1
   ```

## Known limitations

The full LLM planner, optional review retrieval, durable checkpoints, Neo4j,
GraphRAG, MCP, SIEM/Splunk, actions, report endpoints, and long-term memory are not
implemented. BGE model availability and real retrieval quality
must be validated later in a controlled environment before enabling RAG in product.
