# Neo4j Graph Enrichment and GraphRAG

Status: living architecture record. Update this document whenever graph schema,
topology synchronization, enrichment, scheduling, graph capabilities, GraphRAG
retrieval, or Copilot evidence integration changes.

```text
Phase 1       DONE
Phase 2       DONE
Phase 2.5     PASS
Phase 3       DONE
Phase 3.5     DONE
Phase 4       NEXT
```

## 1. Purpose

Neo4j is Soorin Copilot's operational organizational graph and the intended
structural substrate for future GraphRAG. Product remains the authority for
topology and asset facts. Neo4j holds a versioned, query-bounded projection;
Copilot consumes normalized evidence through repository, service, capability,
and EvidencePack boundaries. Qdrant continues to own semantic security
knowledge rather than operational topology.

## 2. Scale Targets

The design target is at least 1,000,000 `Asset` nodes, 10,000,000
`COMMUNICATES_WITH` relationships, and 15 or more Product-derived properties per
Asset. Every implementation must therefore preserve bounded memory, keyset
pagination, parameterized batch writes, indexed retrieval, controlled Product
concurrency, idempotency, active-version targeting, and partial-failure/LKG
tolerance. Full-graph loading and offset pagination are not acceptable runtime
patterns.

## 3. Current Topology Plane

```text
Product GET /zeek/connections/unique-ip-pairs
→ GraphRefreshService
→ normalize and validate
→ versioned Neo4j staging projection
→ validate counts
→ publish GraphMetadata.active_graph_version
→ retire inactive projection
```

Topology refresh runs every 3,600 seconds (one hour), with startup refresh and
jitter controlled by configuration. A candidate projection is invisible until
validation succeeds. Fetch, write, or validation failure leaves the previously
published projection readable, so `active_graph_version` always denotes the
last-known-good topology rather than a staging version.

## 4. Current Enrichment Plane

```text
FastAPI startup
→ GraphEnrichmentRuntimeService managed scheduler
→ acquire graph_enrichment_scheduler lease
→ select prioritized due page from active Neo4j Assets
→ AssetEnrichmentService bounded worker page
→ process-local Product lock
→ acquire graph_enrichment_product_request lease
→ ProductApiClient.get_asset_detection_overview
→ GET /asset-detection/{ip}/overview
→ ProductAssetDetectionOverview DTO
→ AssetEnrichmentMutation normalization
→ Neo4jGraphRepository.apply_enrichment_batch
→ parameterized UNWIND update of active Asset
```

Product overview concurrency is exactly one across scheduled, manual, and
on-demand work. All service instances in one process share the same request
lock, and all processes contend for the same expiring Neo4j Product lease. A
worker invocation processes one priority-aware keyset page and returns an
opaque resume cursor; it is not a load-all sweep. Successful enrichment has a
259,200-second (72-hour) freshness target.

The runtime owns `start()`, `stop()`, `wake()`, one bounded cycle, runtime
status, scheduler ownership, and cached backlog gauges. Each cycle processes at
most `SOORIN_GRAPH_ENRICHMENT_MAX_PAGES_PER_CYCLE`; every page remains bounded
by `SOORIN_GRAPH_ENRICHMENT_PAGE_SIZE`. It starts each runtime page from the
durable Neo4j due set, so process memory and cursors are optimizations rather
than correctness checkpoints. Missing or pending Assets sort before stale/due
Assets, with deterministic graph-key order inside each priority. In a
multi-page cycle the final reserved page reverses that priority, so sustained
new-node intake cannot starve stale work; if no stale work exists, pending work
still fills the page.

FastAPI startup starts the scheduler thread only when enrichment is enabled and
does not wait for Product work. Shutdown signals stop, wakes idle waits, starts
no additional page, lets the current bounded page preserve its write contract,
releases ownership, and joins with the configured timeout. Scheduler failures
are logged and measured at the runtime boundary; they do not terminate FastAPI,
topology refresh, chat, or graph reads.

## 5. Exact Product / Neo4j Schema Mapping

| Product overview field | Neo4j `Asset` property | Normalization |
|---|---|---|
| `assetName` | `asset_name` | trimmed string |
| `ip` | `ip` | canonical IP string; must match requested Asset |
| `status` | `status` | trimmed string |
| `suggestedType` | `suggested_type` | trimmed string |
| `modelConfidence` | `model_confidence` | finite float in `[0, 1]` |
| `mappingConfidence` | `mapping_confidence` | finite float in `[0, 1]` |
| `unknownScore` | `unknown_score` | finite float in `[0, 1]` |
| `classificationSummary` | `classification_summary` | trimmed string |
| `vendor` | `vendor` | trimmed string |
| `product` | `product` | trimmed string |
| `role` | `role` | trimmed string |
| `roles` | `roles` (`roles[]`) | ordered list of trimmed strings |
| `tag` | `tag` | trimmed string |
| `subTag` | `sub_tag` | trimmed string |
| `lastDetectionAt` | `last_detection_at` | timezone-aware UTC ISO-8601 string |

Missing/null optional Product values remain unknown and are not invented.
Success writes also maintain `enrichment_status`, `enrichment_updated_at`,
`enrichment_last_attempt_at`, `enrichment_last_success_at`,
`enrichment_next_due_at`, `enrichment_source`, `enrichment_version`, and a
cleared `enrichment_error`. The version is a SHA-256 hash of canonical
enrichment properties.

## 6. Enrichment State / Failure Semantics

Implemented states are:

| State | Meaning |
|---|---|
| `pending` | Active topology Asset has no completed enrichment yet. |
| `success` | Current Product properties were validated and persisted. |
| `stale` | A later attempt failed but prior valid properties remain available. |
| `error` | Attempt failed before any successful enrichment existed. |
| `unavailable` | Product reported no usable overview before any success existed. |

Failure never erases previous valid enrichment. It updates attempt, next-due,
source, error, and status metadata only. A transient Product API error becomes
eligible after `SOORIN_GRAPH_ENRICHMENT_RETRY_SECONDS` (default 3,600 seconds).
Unavailable or contract-invalid responses use the 72-hour refresh cadence.
HTTP retries/backoff remain owned by the shared Product client; the worker does
not add nested HTTP retries.

## 7. Topology / Enrichment Version Interaction

```text
published V1 Asset + valid enrichment
→ build matching V2 staging Asset
→ copy all enrichment properties and operational metadata
→ validate V2 topology
→ publish V2
→ retire V1
```

New V2 Assets without a V1 match start as `pending`. Carry-forward separates
the one-hour topology cadence from the 72-hour enrichment cadence: publishing
fresh topology must not erase still-valid Product enrichment. Topology
publication and enrichment writes share an in-process graph mutation lock.
Staging writes return the number of new pending Assets without constructing a
second million-node Python set. After V2 is published and inactive versions are
retired, a non-blocking callback wakes the enrichment runtime when this count is
positive. Enrichment never blocks topology publication.

## 8. Runtime Ownership, Recovery, and Operations

`SoorinRuntimeLease` nodes are protected by a unique lease-name constraint.
Acquire, renew, release, and expiry recovery each use a short transaction. No
Neo4j transaction remains open during Product HTTP. The scheduler lease is held
and renewed by one API process; peers keep serving requests and retry ownership
after the poll interval. A crashed owner is recoverable after lease expiry, and
a non-owner cannot release the current lease.

The Product request lease is separate from scheduler ownership. Every actual
overview request enters both the process-local lock and this distributed lane,
which preserves global Product overview concurrency `1` even when a scheduled
worker in one process competes with manual or on-demand work in another.

Authenticated operational routes are:

- `GET /graph/enrichment/status`: cached lifecycle, ownership, progress, and
  backlog status; it does not scan Neo4j.
- `POST /graph/enrichment/wake`: interrupt idle polling and request another
  bounded scheduled cycle.
- `POST /graph/enrichment/assets/{ip}?mode=refresh_if_stale|force_refresh`:
  execute the canonical single-Asset path. Fresh `refresh_if_stale` calls skip
  Product; stale, pending, and missing enrichment refresh; `force_refresh`
  always refreshes an active Asset. Missing active Assets return without a
  Product request.

The same `AssetEnrichmentService.enrich_asset` operation is the future
on-demand integration boundary; no second DTO, normalizer, Product client, or
write path exists.

## 9. Configuration

These are application defaults from `Settings`; deployment examples may set
stricter bounds.

### Product and Neo4j

| Variable | Default | Purpose |
|---|---:|---|
| `SOORIN_PRODUCT_TOPOLOGY_PATH` | `/zeek/connections/unique-ip-pairs` | Authoritative topology endpoint. |
| `SOORIN_PRODUCT_ASSET_DETECTION_OVERVIEW_PATH` | `/asset-detection/{ip}/overview` | Compact enrichment endpoint. |
| `SOORIN_NEO4J_URI` | `bolt://127.0.0.1:7687` | Neo4j Bolt URI. |
| `SOORIN_NEO4J_USER` | `neo4j` | Neo4j user. |
| `SOORIN_NEO4J_PASSWORD` | empty/required at runtime | Neo4j secret; never document its value. |
| `SOORIN_NEO4J_DATABASE` | `neo4j` | Explicit database. |
| `SOORIN_NEO4J_QUERY_TIMEOUT_SECONDS` | `8` | Bounded query timeout. |
| `SOORIN_NEO4J_SYNC_BATCH_SIZE` | `1000` | Topology write batch size. |
| `SOORIN_NEO4J_MAX_CONNECTION_POOL_SIZE` | `50` | Driver pool bound. |

### Enrichment

| Variable | Default | Purpose |
|---|---:|---|
| `SOORIN_GRAPH_ENRICHMENT_ENABLED` | `false` | Enables managed scheduled and explicit enrichment operations. |
| `SOORIN_GRAPH_ENRICHMENT_CONCURRENCY` | `1` | Only supported Product request concurrency. |
| `SOORIN_GRAPH_ENRICHMENT_BATCH_SIZE` | `100` | Maximum mutations per Neo4j `UNWIND` transaction. |
| `SOORIN_GRAPH_ENRICHMENT_PAGE_SIZE` | `500` | Maximum Assets in one keyset page. |
| `SOORIN_GRAPH_ENRICHMENT_REFRESH_SECONDS` | `259200` | Successful enrichment freshness target. |
| `SOORIN_GRAPH_ENRICHMENT_RETRY_SECONDS` | `3600` | Transient Product failure retry eligibility. |
| `SOORIN_GRAPH_ENRICHMENT_POLL_INTERVAL_SECONDS` | `60` | Maximum idle wait between bounded cycles; wakes interrupt it. |
| `SOORIN_GRAPH_ENRICHMENT_STARTUP_DELAY_SECONDS` | `5` | Delay before background ownership/work begins. |
| `SOORIN_GRAPH_ENRICHMENT_MAX_PAGES_PER_CYCLE` | `4` | Hard page bound for one scheduler cycle. |
| `SOORIN_GRAPH_ENRICHMENT_LEASE_TTL_SECONDS` | `900` | Renewable scheduler and Product lease expiry. |
| `SOORIN_GRAPH_ENRICHMENT_SHUTDOWN_TIMEOUT_SECONDS` | `30` | Managed scheduler join timeout. |

### Phase 2.5 validation-only controls

`app/src/tests/phase25_product_validation.py` additionally requires
`SOORIN_PHASE25_ALLOW_ISOLATED_MUTATION=1` and separate
`SOORIN_PHASE25_NEO4J_URI`, `SOORIN_PHASE25_NEO4J_USER`, and
`SOORIN_PHASE25_NEO4J_PASSWORD` values. The target URI must be loopback and must
differ from the configured source Neo4j URI. `SOORIN_PHASE25_SAMPLE_SIZE`
defaults to `8` and accepts only `5` through `10`.

The Community suite uses the standard opt-in flag:

```bash
SOORIN_RUN_NEO4J_INTEGRATION=1 \
SOORIN_NEO4J_URI=bolt://127.0.0.1:<isolated-port> \
SOORIN_NEO4J_USER=neo4j \
SOORIN_NEO4J_PASSWORD='<isolated-password>' \
.venv/bin/python -m pytest app/src/tests/test_neo4j_community_parity.py -q
```

Never point these mutating validation paths at a production database.

### Graph query policy

| Variable | Default |
|---|---:|
| `SOORIN_GRAPH_RAW_PATH` | `data/raw/topology_raw.json` |
| `SOORIN_GRAPH_MAX_UI_NODES` | `1000` |
| `SOORIN_GRAPH_DEFAULT_MIN_DEGREE` | `1` |
| `SOORIN_GRAPH_API_MAX_NEIGHBORS` | `1000` |
| `SOORIN_GRAPH_DEFAULT_SCOPE` | `node_summary` |
| `SOORIN_GRAPH_ONE_HOP_MAX_NODES` | `500` |
| `SOORIN_GRAPH_FULL_NEIGHBORS_HARD_MAX` | `5000` |
| `SOORIN_GRAPH_TWO_HOP_MAX_NODES` | `1000` |
| `SOORIN_GRAPH_MAX_EDGES` | `5000` |
| `SOORIN_GRAPH_MAX_CONTEXT_TOKENS` | `8000` |
| `SOORIN_GRAPH_MAX_PATH_LENGTH` | `24` |
| `SOORIN_GRAPH_FULL_ENUMERATION_MAX_PEERS` | `100` |
| `SOORIN_GRAPH_CONTEXT_MAX_ENUMERATED_NODES` | `250` |
| `SOORIN_GRAPH_CONTEXT_MAX_ENUMERATED_EDGES` | `500` |
| `SOORIN_GRAPH_COMPARISON_MAX_PEERS_PER_ENTITY` | `100` |
| `SOORIN_GRAPH_COMPARISON_MAX_SHARED_PEERS` | `100` |

### Topology refresh

| Variable | Default |
|---|---:|
| `SOORIN_GRAPH_AUTO_REFRESH_ENABLED` | `true` |
| `SOORIN_GRAPH_REFRESH_INTERVAL_SECONDS` | `3600` (minimum `600`) |
| `SOORIN_GRAPH_REFRESH_ON_STARTUP` | `true` |
| `SOORIN_GRAPH_REFRESH_STARTUP_DELAY_SECONDS` | `5` |
| `SOORIN_GRAPH_REFRESH_JITTER_SECONDS` | `30` |
| `SOORIN_GRAPH_REFRESH_MAX_CONSECUTIVE_FAILURES` | `5` |
| `SOORIN_GRAPH_REFRESH_KEEP_RAW_SNAPSHOTS` | `5` |
| `SOORIN_GRAPH_SNAPSHOT_TTL_HOURS` | `72` |
| `SOORIN_GRAPH_REFRESH_LOCK_TIMEOUT_SECONDS` | `60` |
| `SOORIN_GRAPH_REFRESH_MIN_NODES` | `1` |
| `SOORIN_GRAPH_REFRESH_MIN_EDGES` | `0` |

## 10. Operational Observability

The existing `SoorinMetrics` singleton registry and authenticated `/metrics`
exposition now include cycle started/completed/failed counters, Asset
attempted/succeeded/failed/unavailable/updated/skipped counters, Product
overview request outcomes, cycle duration, Product overview latency, scheduler
running/ownership gauges, cached pending/stale/error/unavailable/backlog gauges,
and last-run/last-success timestamps.

Metric labels are limited to `trigger=scheduled|manual|on_demand`, bounded
outcomes, and the five fixed backlog states. Asset identity, IP, graph key,
request ID, URL, and error text are never labels. Backlog counts are refreshed
once after a cycle and cached in gauges; `/metrics` performs no graph scan.

## 11. Current Capabilities

The stable public capabilities are `graph.get_summary`,
`graph.get_neighbors`, `graph.get_relationship`, `graph.find_path`, and
`graph.compare_assets`. Each flows through capability execution and graph
context/service policy to `Neo4jGraphRepository`; Neo4j sessions, Cypher, and
driver objects do not leak into capability or EvidencePack contracts. Direction,
hop, node, edge, peer, path, and query-time bounds are enforced by
`GraphQueryPolicy` and parameterized repository queries.

## 12. GraphRAG Retrieval Roadmap

### Phase 1 — DONE: Asset Enrichment Foundation

DTO validation, canonical normalization, Neo4j Asset properties, batch upsert,
configuration, and offline tests are complete.

### Phase 2 — DONE: Scalable Enrichment Worker

Active-version keyset enumeration, Product concurrency `1`, partial error
handling, LKG behavior, bounded batch writes, and topology-version carry-forward
are complete.

### Phase 2.5 — DONE/PASS: Production and Integration Validation

Neo4j Community schema, persistence, carry-forward, failure, and multi-page
pagination were validated. Eight real Product Assets were requested
sequentially and persisted to an isolated projection with measured latency.

### Phase 3 — DONE: Runtime Scheduling

Managed rolling due-page processing, immediate post-publication new-node wakeup,
manual wake, freshness-aware single-Asset refresh, bounded shutdown, durable
restart discovery, scheduler ownership, and global Product serialization are
implemented.

### Phase 3.5 — DONE: Operational Observability

Low-cardinality lifecycle, throughput, outcome, latency, ownership, progress,
timestamp, and cached backlog metrics are integrated with the existing
Prometheus registry and protected exposition path.

### Phase 4 — NEXT: Exact / Structured Graph Search

Add indexed structured lookup for exact IP, asset name, role, type, status,
OS/platform when authoritative, vendor, product, confidence, grouping/counts,
and missing/stale enrichment, composed with existing bounded traversal. Add
indexes only after query-plan measurement demonstrates the need.

### Phase 5 — PLANNED: Neo4j Full-Text Search

Introduce measured, schema-controlled full-text retrieval over approved Asset
properties.

### Phase 6 — PLANNED: Semantic Asset Search

Define `asset_search_text` and `semantic_content_hash`, produce BGE embeddings,
create a Neo4j vector index, and implement semantic Asset retrieval. Do not
embed raw IPs as semantic content.

### Phase 7 — PLANNED: Hybrid GraphRAG

Combine exact, full-text, and vector seeding with bounded Cypher traversal and
normalized EvidencePack output.

### Phase 8 — PLANNED: Organizational RAG

Unify Neo4j asset/topology evidence, current Product evidence, Qdrant security
knowledge, and provenance-preserving memory/history in one EvidencePack.

### Phase 9 — PLANNED: Advanced Graph Intelligence

Add scheduled community detection, cluster summaries, centrality,
network-wide dependencies, blast radius, organizational questions, and
temporal/history modeling. Expensive analytics remain offline, not chat-time.

## 13. Exact Search Question Coverage

Phase 4 should support questions such as:

- What is `192.168.0.125`?
- Which Assets are Domain Controllers?
- How many Windows systems do we have?
- What are our Linux systems and their IPs?
- Show Linux systems and their connections.
- Which Assets provide LDAP or DNS?
- Which low-confidence Assets have many peers?
- Which Assets have stale enrichment?
- Which topology Assets have no classification?

These are structured lookup/traversal goals, not current Phase 2.5 claims.

## 14. Semantic / Hybrid Search Examples

Future semantic or hybrid retrieval should address questions such as:

- Find systems involved in authentication.
- Find assets similar to Domain Controllers.
- Find critical infrastructure semantically and expand their graph neighborhoods.
- What systems would have the largest blast radius?

These require later full-text/vector/hybrid phases and must not be represented as
current exact graph capability coverage.

## 15. Known Constraints / Risks

- Product overview concurrency is fixed at `1`; raising it fails configuration.
- Sequential throughput is a material 1M-node freshness risk and must be
  monitored under real scheduler conditions.
- IP remains a transitional Asset identity until Product exposes a durable ID.
- Neo4j availability is required to acquire scheduler and Product leases;
  enrichment backs off while the rest of the API remains available.
- The 72-hour value is a freshness target, not a guaranteed full-sweep
  wall-clock SLA.
- Product and database latency may vary; eight requests are directional evidence,
  not a capacity benchmark under sustained load.
- Exact enriched-property search, full-text index, embeddings, vector indexes,
  and semantic GraphRAG remain outside Phase 3/3.5.

## 16. Validation History

### 2026-09-10 — Phase 3 + Phase 3.5 DONE

- Starting revision: `929eb01` (`dev`, equal to `origin/dev`, clean).
- Affected offline coverage: 231 passed, 41 skipped, and 65 subtests passed.
  This included enrichment, runtime lifecycle, Neo4j repository, topology
  refresh integration, API startup/shutdown and auth, Product client contracts,
  metrics, settings, graph API, Streamlit graph compatibility, and context
  routing/capability contracts.
- Neo4j Community `2026.07.1`: 14 passed against a fresh, no-volume isolated
  container. The gate covered the lease-name uniqueness constraint, atomic
  acquire/deny/renew/release, expiry recovery, non-owner release denial,
  all Phase 2.5 enrichment/LKG/carry-forward contracts, and priority-aware
  keyset paging.
- Runtime tests prove disabled/enabled startup, non-blocking startup, clean
  shutdown, wake interruption, scheduler fault containment, exact cycle page
  bounds, stale-work progress, durable due-work rediscovery, all four
  freshness/force cases, post-publication new-node wakeup, and two logical
  owners preserving global Product concurrency `1`.
- Observability tests verify metric families, counters/gauges/histograms,
  bounded trigger/outcome/state labels, and rejection of Asset IP or request ID
  label values. Prometheus exposition reads cached gauges and does not query
  Neo4j.
- `python -m compileall app/src` and `git diff --check` passed.
- Incremental Graphify refresh produced 4,495 nodes, 13,233 edges, and 164
  communities. Path/query checks confirmed startup-to-runtime,
  topology-publication-to-wake, runtime-to-worker, worker-to-Product,
  worker-to-repository, manual/on-demand-to-worker, and runtime-to-metrics
  relationships with no Phase 4 retriever implementation.

### 2026-09-10 — Phase 2.5 PASS

- Starting revision: `2857131` (`dev`). Phase 1/2 implementation revisions:
  `ac9e3de` and `ded5427`.
- Phase 2.5 code/test revisions: `9218364` (Community transaction
  compatibility) and `b61712e` (integration and Product validation coverage).
- Neo4j: `2026.07.1` Community in an isolated no-volume container.
- Community integration: 13 passed. Verified constraints
  `asset_graph_identity`/`graph_metadata_id`, indexes `asset_version_ip` and
  `asset_enrichment_schedule`, all 15 properties via direct Cypher, success and
  failure metadata, LKG, V1→V2 carry-forward, new-node pending behavior,
  missing-node safety, and 54 Assets over 8 keyset pages without duplicates or
  skips.
- A real-driver failure revealed that `ManagedTransaction.run` rejects Neo4j
  `Query` objects. The enrichment write now uses raw parameterized Cypher inside
  `execute_write`; timeout-wrapped `Query` objects remain used for supported
  `session.run` reads.
- Real Product sample: 8 Assets, all successful and updated, with identities
  redacted. Categories: one Domain Controller, three Domain Joined Workstations,
  two Linux Servers, one Firewall, and one Windows Workstation; one Asset had
  `REVIEW` status and seven had `CONFIRMED` status.
- Every sample Asset had all 15 Product properties and all success metadata
  persisted; direct expected/actual comparison passed 8/8. Maximum observed
  Product overview concurrency was `1`.
- Latency seconds per aliased Asset: `0.288`, `0.049`, `0.052`, `0.055`,
  `0.051`, `0.045`, `0.051`, `0.045`. Min `0.045`, average `0.080`, max
  `0.288`, p50 `0.051`, p95 `0.206`, total worker-page time `0.761`.
  Percentiles use linear interpolation over the ordered eight observations.
- Observed throughput: `10.514754` Assets/s, `630.885` Assets/min, and
  `37,853.113` Assets/hour. Linear extrapolations: 400 Assets `38.042s`;
  10,000 Assets `951.045s` (`0.264h`); 1,000,000 Assets `95,104.464s`
  (`26.418h`, `1.101d`). These are extrapolations, not sustained-load SLAs.
- Carry-forward: PASS; all Product properties and operational metadata survived
  V1→V2 publication, while new V2 Assets were pending. Failure/LKG: PASS; prior
  role/vendor/product and success time survived a later safe mocked failure.
- Offline affected suite before integration: 235 passed, 52 skipped, and 74
  subtests passed; one local-only environment-structure test failed because the
  private `.env` lacks the six new enrichment keys. Private values were neither
  read nor changed. Final focused suite after changes: 235 passed, 54 skipped,
  1 explicitly deselected private-environment check, and 74 subtests passed.
- Compileall and `git diff --check`: PASS. Post-change incremental Graphify:
  4,370 nodes, 12,860 edges, and 167 communities. It confirmed the existing
  worker→repository and refresh→repository→mutation paths plus the validator's
  imports of the existing Product client, enrichment service, and repository.
  Graphify reported 85 stale generated community labels; this affects generated
  visualization naming, not source dependency validation.
