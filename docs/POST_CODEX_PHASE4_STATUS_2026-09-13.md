# Post-Codex Phase 4 Current Status — 2026-09-13

This document is the current source-audit addendum for `dev` after commit `2f1902ba2d62ab0b197d3e3b09b219c1df035562` (`fix(phase4): harden structured search continuity and memory safety`). It supersedes older status/count statements in Phase 4 and memory documents where those statements conflict with current source.

## Validation baseline

Before `2f1902b` was pushed, the exact local working tree passed:

- compileall: exit 0;
- focused regression: `392 passed, 41 skipped, 62 subtests`;
- broad Phase 4/memory/router regression: `541 passed, 46 skipped, 62 subtests`.

`dev`, `personal/dev`, and the organization `origin/dev` were confirmed equal at `2f1902b` before this audit.

## Current operational graph boundary

Product remains authoritative for topology source data and current/deep Asset Profile/Detection facts. Neo4j is the bounded organizational discovery/topology projection. Qdrant remains documentation/memory semantic retrieval, and memory remains conversational/historical continuity rather than current operational truth.

The organizational Neo4j projection uses RFC1918 source or destination endpoints as organizational Asset nodes and publishes only a validated active graph version. Refresh failure preserves the last-known-good active projection. Enrichment is independently scheduled, bounded, serialized against Product overview requests, and does not erase prior valid enrichment when a refresh attempt fails.

The chat Graph provider and `/graph` API must use the same `OrganizationalNeo4jGraphRepository` semantics. This keeps RFC1918 endpoint Asset authority and case-insensitive structured text selectors consistent between chat Exact Search and graph APIs.

## Exact Search and natural-language mapping

The structured path is:

```text
natural-language request
→ structured/set-continuation detection
→ bounded field/value normalization
→ confidence percentage/fraction normalization before typed validation
→ StructuredQuerySpec
→ semantic Router or deterministic structured fallback
→ current-set refinement when applicable
→ graph.search_assets / graph.aggregate_assets
→ OrganizationalNeo4jGraphRepository active projection
→ typed structured evidence
→ bounded model context
→ optional Phase4C focal deepening
```

Structured filters are allow-listed. Natural-language text selectors are normalized above the database boundary, while organizational Neo4j text comparison is case-insensitive. Numeric score expressions such as `90`, `90%`, and `0.90` normalize into the typed `0..1` contract. Strict `above` / `below` semantics remain strict.

Structured-looking requests that cannot be represented safely fail closed/clarify rather than falling through to a focal Asset investigation.

## IP identity contract

IP identity is mandatory in structured discovery:

- every `StructuredAssetRow` carries `ip`;
- Neo4j search explicitly returns `a.ip AS ip`;
- every search row included in Synth model context carries `ip`;
- grouped aggregation preserves exact group counts plus a bounded sample of canonical `member_ips`;
- the aggregate member-identity cap is 20 IPs per group;
- `member_ips_truncated` indicates when the group contains more identities than that retrieval cap;
- model serialization must not silently reduce that already-bounded member-IP sample further.

Search/model context itself is still bounded: not every matched row is necessarily serialized. Coverage fields expose matched, retrieved, included, and omitted counts. The invariant is that every row/group member identity actually included in the structured context retains its IP.

## Structured set continuity and focal continuity

A structured result set is not a conversational focal entity. Search/aggregate rows do not mutate the active entity merely because they were returned.

`StructuredQueryContext` is persisted separately for set-level follow-ups such as:

- `which of them ...`;
- `group them by vendor`;
- refinements such as confidence thresholds;
- bounded selection of retained prior results.

Incidental UI selection is suppressed for global structured-set requests and set continuations. Explicit IPs in the current message remain stronger than UI-selected and active-session entities.

Phase4C deepening is a separate focal path. When a search deterministically selects one or two Assets for requested analysis, the focal Asset(s) receive normal focal investigation state/baseline semantics while the `StructuredQueryContext` remains available for later set refinement. This is deliberate dual continuity, not one state overwriting the other.

## Memory layers

Current memory responsibilities remain distinct:

- recent/short-term turns: bounded routing and conversation continuity;
- Working Memory: bounded user-provided facts and current episode state;
- episodic/thread state: entity timeline, summaries, baselines, structured-query continuity and routing state;
- Product LTM: canonical durable LTM in Product-backed mode;
- Qdrant memory index: derivative semantic index, never canonical LTM authority.

For global structured queries and structured-result continuations, preliminary entity-scoped LTM is bypassed or removed so a stale/UI-selected Asset cannot contaminate the set query before final routing authority is known.

Product LTM summary parsing and canonical hydration are hardened and expose stage-specific `ProductMemoryContractError` diagnostics. This source hardening is implemented, but the previously observed live Product DTO failure should remain an E2E acceptance item until the same real backend records are exercised successfully after restart.

Whole-thread exhaustive recall remains bounded by design; this Phase 4 hardening does not turn memory into an unlimited transcript store.

## Entity authority

For ordinary focal resolution the deterministic precedence remains:

```text
explicit IP in current prompt
> selected UI IP
> active session entity/pair
```

Structured-set authority is orthogonal: a global set request or recognized set continuation suppresses incidental UI/active focal inheritance. A later explicit selection/deepening can then enter the existing one- or two-entity focal workflow.

## Router and Synth status

Router transport failure, malformed output, schema-invalid structured output, and `finish_reason=length` are distinct failure classes. Transport failures do not receive meaningless content repair; safe deterministic structured fallback is used when available.

Synth receives deterministic aggregate percentages rather than calculating them itself, and prompt constraints distinguish observed facts, inference, hypotheses, unknowns, bounded identity samples, and implementation jargon.

Large Router/Synth latency and token cost remain optimization work; this hardening primarily targets correctness and continuity.

## Remaining acceptance items

Highest-value live tests after rebuild/restart remain:

```text
show me asset with confidence above 90

find all Linux Servers
→ which of them has model confidence under 90?

find all Linux Servers
→ group them by vendor

group all assets by vendor
→ which IPs are in VMware?

Find the highest-confidence firewall and analyze its current detection state comprehensively.

can you tell me what we discussed and investigated at all?
```

Inspect runtime logs for structured-result references, active-entity before/after state, focal investigation baseline writes, Product-memory contract failures, LTM candidate/inventory behavior, and Router fallback classification.

## Current status

```text
Neo4j versioned organizational projection      implemented / live-validated previously
Exact Search typed retrieval                    implemented
NL structured normalization                     hardened
Structured set continuation                     hardened
Aggregate count + member IP evidence             implemented
Phase4C bounded focal deepening                  implemented
Set + focal dual continuity                      hardened
Pre-route structured/LTM isolation               hardened
Product LTM adapter                              source-hardened; live confirmation remains
Whole-thread exhaustive recall                   intentionally bounded / not fully solved
Router/Synth performance optimization            deferred
Hybrid vector/full-text Neo4j GraphRAG           future work
```
