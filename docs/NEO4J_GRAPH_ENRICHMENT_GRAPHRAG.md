# Neo4j Graph, Asset Enrichment, and Graph-Aware Retrieval

This document describes the current Neo4j organizational/topology projection used by Soorin Copilot, including synchronization, asset enrichment, structured retrieval, graph capabilities, and model-facing context.

## Purpose

Product remains the authoritative source for topology and current Asset/Detection evidence. Neo4j provides a queryable, versioned projection optimized for organizational discovery and bounded graph reasoning.

The graph is used for:

- asset identity/topology summaries;
- inbound, outbound, and bidirectional neighbor analysis;
- direct relationship checks;
- path analysis;
- two-asset topology comparison;
- structured inventory search;
- count and grouped inventory aggregation;
- graph-aware context for cybersecurity analysis.

## Runtime

The current deployment uses:

```text
Neo4j Community 2026.07.1
```

The API connects over Compose networking with:

```text
bolt://neo4j:7687
```

Host Bolt publishing is independently controlled by deployment environment variables and normally remains loopback-only.

## Authority Model

```text
Product topology / detection
        ↓
validated synchronization and enrichment
        ↓
versioned Neo4j projection
        ↓
read-only Copilot graph capabilities
```

Neo4j is an organizational projection. Current deep Asset Profile and Detection facts remain Product evidence.

## Versioned Projection

Graph synchronization builds/publishes a versioned projection. Queries target only the active published version. Refresh failure does not replace the active graph with an incomplete projection; the previous known-good version remains available.

The graph service tracks projection metadata and exposes retrieval completeness so downstream evidence review and context composition can distinguish complete requested scope from bounded broader retrieval.

## Topology Refresh

Automatic graph refresh is controlled through the graph refresh settings in `.env.example`, including:

- enable/disable;
- refresh interval;
- startup refresh behavior and delay;
- jitter;
- maximum consecutive failures;
- raw snapshot retention;
- snapshot freshness TTL;
- refresh lock timeout;
- minimum acceptable node/edge counts.

Product topology is fetched through the configured Product topology path and normalized before publication.

## Asset Enrichment

Topology alone provides network relationships. The enrichment runtime augments Asset nodes with approved Product Detection overview fields used for discovery and classification.

The enrichment worker is independently configurable and bounded by:

- enabled state;
- concurrency;
- batch/page size;
- refresh eligibility;
- failure retry eligibility;
- polling interval;
- startup delay;
- pages per cycle;
- lease TTL;
- shutdown timeout.

Enrichment writes are part of graph synchronization/maintenance, not user-issued graph query capabilities.

## Structured Asset Search

`graph.search_assets` performs typed structured discovery over approved fields. The query contract does not expose raw Cypher or arbitrary property access.

Supported selector families include:

- IP;
- asset name;
- inventory/classification status;
- suggested type;
- primary role;
- roles membership;
- vendor;
- product;
- tag/sub-tag;
- enrichment status;
- bounded confidence ranges;
- bounded timestamp ranges.

Sorting and limits are also constrained to approved fields and configured bounds.

Search evidence records:

- normalized filters;
- query identity;
- active graph version;
- matched total;
- returned rows;
- retrieval truncation;
- retrieval timestamp.

## Structured Aggregation

`graph.aggregate_assets` supports approved count/group-count operations over typed filters and fixed grouping fields.

Aggregate evidence carries:

- normalized filters;
- query identity;
- operation;
- grouping field(s);
- total count;
- returned groups;
- bounded member-IP samples where requested;
- percentages where produced by the capability;
- truncation/completeness metadata;
- active graph version.

## Model-Facing Structured Context

Neo4j match counts and the model-facing serialization budget are separate concerns.

The current configuration includes:

```text
SOORIN_CONTEXT_STRUCTURED_ASSET_SEARCH_MAX_TOKENS
SOORIN_CONTEXT_STRUCTURED_ASSET_AGGREGATE_MAX_TOKENS
SOORIN_CONTEXT_STRUCTURED_ASSET_SEARCH_MAX_ROWS
```

These values bound how much structured evidence is serialized into the LLM context; they do not change the underlying Neo4j matched count.

The global LLM context budget still applies after the structured-source cap.

## Graph Summary

`graph.get_summary` returns the bounded graph identity/topology view for a focal asset, including degree/direction information and relevant node metadata from the active projection.

This is commonly used during deep single-asset investigation alongside Product Profile and Detection evidence.

## Neighbor Analysis

`graph.get_neighbors` supports bounded scopes and directions. Graph settings control one-hop, two-hop, full-neighbor, node, and edge limits.

The result distinguishes retrieval completeness from serialization completeness so a downstream answer can report whether the requested scope was fully represented.

## Direct Relationships

`graph.get_relationship` checks the direct graph relationship between selected entities and returns bounded relationship evidence without exposing arbitrary graph queries.

## Asset Comparison

`graph.compare_assets` compares two focal assets using deterministic graph evidence such as degree, peer sets, shared peers, and bounded topology summaries. Per-entity and shared-peer enumeration are controlled by configured graph comparison limits.

## Path Search

`graph.find_path` performs bounded path discovery with the configured maximum path length. It is a semantic capability, not a raw Cypher endpoint.

## Discovery-to-Investigation Flow

A structured search can feed a deeper investigation without turning every result into an expensive Product fan-out:

```text
Asset search
→ bounded result set
→ deterministic candidate/focal selection
→ when selection is unambiguous and analysis is requested:
     Product Profile
     Product Detection
     Graph summary/topology
→ EvidencePack
→ grounded answer
```

Large or ambiguous sets remain set-level results.

## StructuredQueryContext

After a structured search/aggregate turn, a bounded `StructuredQueryContext` can be persisted in ThreadState. It records result-set lineage and bounded references for follow-up/refinement workflows without converting every result into an active conversational entity.

## Evidence Semantics

Graph capability results are normalized into `ToolResult`/`EvidenceReceipt` metadata containing:

- source capability;
- entities;
- active graph/query identity where applicable;
- freshness;
- completeness;
- truncation;
- included/omitted counts;
- retrieval timestamp;
- safe limitations.

The Evidence Reviewer checks the graph evidence against the validated task before it is treated as sufficient for synthesis.

## Graph Context Budgets

Different graph scopes have bounded model-facing token budgets. Configuration also limits:

- maximum API neighbors;
- one-hop and two-hop node counts;
- hard full-neighbor node count;
- maximum edges;
- overall graph context tokens;
- full-enumeration peer counts;
- maximum enumerated nodes/edges;
- comparison peer counts.

This keeps graph reasoning bounded even when the underlying topology is large.

## Operational Status Fields

The graph enrichment projection can contain inventory/classification fields such as `CONFIRMED`, confidence, roles, product, and vendor. These are not automatically the same concept as a Product Profile's operational activity/status fields. Copilot preserves source provenance so those dimensions can be interpreted separately.

## Persistence and Backup

Neo4j data and logs are stored in named Docker volumes. Routine API/UI deployment does not remove the Neo4j volume. Product remains the rebuild authority for topology, but the Neo4j volume should still be treated as operational state and backed up according to server policy.

## Security Boundary

The Copilot capability surface is read-only for analyst requests. Structured query validation, allow-listed properties, bounded scope, and application-side task/evidence validation prevent an LLM from turning user text into arbitrary Neo4j mutation or raw query execution.