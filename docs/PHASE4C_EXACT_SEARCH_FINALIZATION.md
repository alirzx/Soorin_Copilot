# Phase 4C — Exact Search Finalization

Status: **IMPLEMENTED on `dev` pending final CI confirmation**

Phase 4C turns structured Neo4j Asset discovery into a bounded investigation entry point without changing Product API contracts, frontend contracts, public chat schemas, Neo4j schema, or the two-focal-entity limit.

## Final flow

```text
User request
→ deterministic entity/request authority
→ Semantic Router
→ StructuredQuerySpec
→ graph.search_assets / graph.aggregate_assets
→ bounded Asset-set evidence
→ deterministic candidate selection (search only, max 1–2)
→ Product Profile / Detection for selected focal Assets only
→ bounded Graph topology for selected focal Assets when needed
→ optional Knowledge when explicitly useful
→ unified EvidencePack
→ deterministic review
→ bounded ContextComposer
→ Synthesizer
→ bounded continuity update
```

The set query remains a zero-entity discovery operation. Returned search rows never become active conversational entities merely because they were retrieved.

## Router hotfix

The Router prompt contract emits `structured_result_reference: null` on ordinary routes. The structured validator now parses that extension field and removes it from the legacy/set validation view before delegation. This keeps normal routes, structured routes, and the repair path aligned with the prompt contract instead of rejecting a prompt-compliant null field as unexpected.

## Exact-search contract

`graph.search_assets` queries only the currently published Neo4j graph version and returns a typed `StructuredAssetRow` projection. Supported user-facing selectors remain bounded and allow-listed:

- exact: `ip`, `asset_name`, `status`, `suggested_type`, `role`, `roles` membership, `vendor`, `product`, `tag`, `sub_tag`, `enrichment_status`;
- numeric ranges: `model_confidence`, `mapping_confidence`, `unknown_score`;
- temporal range: `last_detection_at`;
- sorting: `graph_key`, `ip`, `asset_name`, `model_confidence`, `mapping_confidence`, `unknown_score`, `last_detection_at`, `enrichment_next_due_at`;
- aggregation/grouping: bounded count/group-count over the existing group allow-list.

Returned rows also carry projection/enrichment metadata such as classification summary and enrichment timestamps/source/version. These are evidence metadata, not automatically user-searchable selectors. `graph_version` remains runtime authority rather than a user filter. This preserves the exact-search contract without exposing raw properties, arbitrary operators, regex, OR expressions, raw Cypher, or scheduler-internal controls.

## Deterministic focal selection

Automatic deepening is allowed only when the user actually requests analysis/investigation and the selection is deterministic:

- zero candidates: no deepening;
- one candidate: select that Asset;
- exactly two candidates plus comparison intent: select both;
- more than two candidates: require explicit first/top/ranked-one or ranked-two semantics;
- ranking words such as highest/lowest require a structured sort encoded by the Router;
- ambiguous multi-candidate requests fail closed and remain set-level answers.

At most two Assets are selected. There is no `N search results → N Product calls` path.

## Cross-source deepening

For selected focal Assets, Phase 4C reuses the existing registered read-only capabilities. It does not create a new LLM or provider contract.

- `asset.get_profile`: current/deep Product truth for selected Assets;
- `asset.get_detection`: current classifier/rule/signal evidence when the task needs detection/security analysis;
- `graph.get_summary` or `graph.compare_assets`: bounded topology when the task needs topology/behavior or comparison;
- `knowledge.search`: optional and only when the request calls for background/hardening/runbook/response guidance.

Product evidence remains authoritative for current/deep Asset facts. Neo4j enrichment remains organizational discovery/topology evidence and never substitutes for missing Product evidence.

## Call and fan-out bounds

The existing global capability-call budget remains authoritative. Phase 4C subtracts the Stage-1 search call from the remaining budget before compiling focal deepening. Optional Knowledge is dropped first if needed. If required focal verification cannot fit, deepening fails closed and the structured discovery result remains answerable on its own.

Product capabilities retain one-entity-per-call semantics, so a two-Asset comparison uses at most two profile calls and two detection calls. It never fans out across the full result set.

## Evidence and context

Stage-1 search evidence and Stage-2 focal evidence remain separate first-class `ToolResult` records. They are merged only in `EvidencePack`, preserving native provider provenance.

The reviewer treats Product/Detection failures for selected focal Assets as material limitations. Graph projection is not accepted as a substitute for missing Product truth. Optional Knowledge failure remains a caveat rather than an operational fact failure.

Context remains bounded. The structured set serializer provides a limited discovery summary; Product/Detection/topology context is included only for the selected one or two focal Assets. The Synthesizer is explicitly instructed to distinguish:

```text
discovery result
vs
verified focal-Asset analysis
```

and never project focal findings onto the entire discovered set.

## Memory and continuity

Phase 4B.4 `StructuredQueryContext` remains the result-set continuity mechanism. It retains bounded ordered refs and query identity for follow-ups such as “the first one” or “the first two.”

Automatic Phase 4C focal selection is execution-local and does not itself replace the active entity or pair. Structured-search turns are excluded from focal investigation-baseline capture so one or two auto-selected Assets are not written under a zero-entity search baseline. Existing Working Memory, episodic memory, LTM lifecycle, explicit entity authority, and active-single/active-pair behavior are otherwise unchanged.

Explicit message IP authority remains stronger than structured-result selection when both are present. Existing 4B.4 semantics continue to govern explicit result-set references.

## Observability

Phase 4C emits bounded logs for:

- candidates found;
- candidates selected;
- deterministic selection reason;
- retrieval truncation;
- Product call count;
- topology call count;
- Knowledge call count;
- partial focal failures;
- focal evidence-review limitations.

No sensitive payloads or raw credentials are added to these events.

## Prompt audit

The Router static contract already says structured search/aggregate selectors are not entities and Product/Detection must remain off until a focal Asset is selected later by the workflow. The Planner already receives the typed capability catalog and structured set tasks use deterministic direct planning, so no additional Planner LLM behavior was required.

The Synthesizer `asset_search` task module was updated minimally so user-facing answers separate discovery evidence from verified focal evidence and retain Product/Neo4j authority boundaries. The static Synth system core already contains the necessary evidence, temporal, memory, and provider-precedence rules and was intentionally not enlarged.

## Production boundaries

Phase 4C does not change:

- Product backend endpoints or payload contracts;
- frontend request/response contracts;
- chat JSON/SSE schema;
- Neo4j database schema;
- graph publication/versioning semantics;
- enrichment scheduler or lease semantics;
- Qdrant collection contracts;
- canonical memory schemas;
- Router/Planner/Synth LLM role count.

Graph search and topology continue through `GraphContextProvider → GraphService → Neo4jGraphRepository`, and the enrichment/snapshot runtime continues to publish only validated graph versions.

## Validation gate

The Phase 4 validation workflow now includes Router null-contract regressions and deterministic Phase 4C candidate/fan-out tests in addition to the existing structured Neo4j integration, read-only EXPLAIN/PROFILE audit, and Neo4j Community parity suite.

Phase 4C is considered complete only when this updated workflow passes on the final `dev` commit.