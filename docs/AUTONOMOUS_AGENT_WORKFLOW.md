# Bounded Adaptive Investigator Workflow

## Purpose and rollout

Adaptive mode exists for supported investigations whose next useful evidence request depends on the result of the previous request. It is not an open-ended agent or a general tool-calling surface. Deterministic application code owns routing, authority, validation, budgets, execution, stopping, and persistence.

The runtime has three orchestration modes:

- `direct`: deterministic plan for simple or already-structured work;
- `fixed`: one bounded Planner proposal, validated before execution;
- `adaptive`: a bounded Investigator loop that selects one validated evidence action at a time.

`SOORIN_ADAPTIVE_AGENT_ENABLED=false` is the default. With the flag off, the selector preserves the existing direct/fixed behavior. Even with the flag on, simple, aggregate, memory-only, and no-live requests stay out of the loop. Eligible open-ended multi-step investigations and structured searches that require result-dependent focal deepening may use it.

## Memory-first ordering

Thread state, transcript context, working/episodic memory, eligible long-term memory, entities, and request constraints are restored before routing or orchestration selection. The deterministic evidence-gap planner marks requirements already satisfied by authorized memory. Historical memory cannot satisfy a gap that requires current Product or Graph evidence. The adaptive loop receives only the remaining gaps and safe memory metadata; it does not receive raw memory payloads.

## Investigator contract

The Investigator is a separately configured LLM role. It receives a compact JSON context and returns exactly one strict JSON object:

- `CONTINUE`: one gap ID and a bounded list of capability requests;
- `FINISH`: a declared stop reason;
- `CLARIFY`: a safe clarification code and summary.

Markdown, extra fields, malformed JSON, arbitrary tools, arbitrary Cypher, new entities, budget changes, and free-form answers are rejected. The model's assessment text is not treated as reasoning authority and is not persisted or exposed in Human Trace.

## Evidence ledger and gaps

`EvidenceLedger` is the bounded, request-local, model-safe state of the investigation. It records authorized entities, bounded structured-search candidates, attempted capabilities, safe coverage summaries, contradictions, limitations, failures, gap state, canonical action fingerprints, and typed `EvidenceReference` entries. Raw provider payloads remain in immutable `ToolResult`, `EvidenceReceipt`, and final evidence-pipeline objects.

An `EvidenceReference` contains only bounded metadata: a deterministic reference ID, context identity, source capability, authorized entity scope, evidence classes and covered gaps, status/freshness/completeness, authority and temporal classes, projection schema version, semantic fingerprint, applicable Product views, structured-query identity, active Graph version, and material limitation flags. It is an index into evidence already held by the request, not a second evidence store and not a new source of authority.

Context identity and semantic identity are deliberately separate. Context identity answers which capability/entity/query/scope produced evidence. The versioned SHA-256 semantic fingerprint answers whether its normalized model-facing content changed, excluding request IDs, trace IDs, step IDs, latency, cache timing, and retrieval timestamps. A stable evidence-reference ID derives from the context identity plus semantic fingerprint. Only short prefixes are rendered in Investigator context and Human Trace.

Each `EvidenceGap` includes:

- importance and status;
- `current`, `historical`, or `either` temporal semantics;
- an allow-list of capabilities that may satisfy it;
- authority such as current Product data, Graph projection, knowledge reference, or historical memory;
- its authorized entity scope.

The runtime, not the Investigator, decides whether evidence satisfies a gap. A successful historical-memory result therefore does not close a current-state gap.

## Topology

```text
restore memory/entity context
  -> route
  -> validate task + deterministic orchestration selection
     -> direct plan -> PlanValidator -> existing execution path
     -> fixed Planner -> PlanValidator -> existing execution path
     -> initialize_agent_loop
        -> evaluate_agent_progress
        -> investigator_decide
        -> validate_agent_action
        -> canonical action fingerprint + equivalence gate
        -> execute_agent_action
        -> stable evidence references + delta observation
        -> update_agent_ledger
        -> evaluate_agent_progress ...
  -> build evidence
  -> deterministic review
  -> compose/review context
  -> existing Synthesizer
  -> final continuity/memory update
```

After one-time initialization, the six-node decision cycle can repeat: progress evaluation, Investigator decision, action validation, capability execution, observation construction, and ledger update. Each accepted action is compiled into an `ExecutionPlan` with `source=investigator`, checked by `AgentActionValidator`, checked again by the existing `PlanValidator`, and run through the existing `CapabilityExecutor`. The Reviewer identifies evidence sufficiency; it does not choose adaptive tools. Legacy supplemental retrieval is disabled only while adaptive mode is active.

## Validation and security boundaries

The action validator enforces:

- registered, Planner-visible, read-only capabilities only;
- capability-to-gap authorization;
- request `allow_live`, memory-only, entity, graph scope/direction/depth, and structured-query authority;
- resolved entities or bounded candidates produced by the current structured search;
- per-decision and cumulative tool limits;
- deepened-entity and request-deadline limits;
- exact/equivalent action repetition protection.

The canonical action fingerprint is computed only after `PlanValidator` has normalized defaults and arguments. Operational identifiers and timeouts are excluded. Entity order is preserved for directional/path semantics and normalized only for explicitly commutative comparison capability contracts.

Equivalence is conservative and capability-specific. Complete usable Product evidence may cover a requested subset of the same capability's views and detail. Graph reuse requires the same action semantics and an active projection version. Structured search/aggregate reuse requires the same semantic query and a known request-local active Graph version. Knowledge reuse applies the same deterministic purpose/query policy as Planner validation. When equivalence is proven, only the selected compatible gap is marked covered and the existing immutable result remains available to final synthesis; no provider call is made. Partial, truncated, contradictory, projection-unusable, source-incomplete, cross-provider, or temporally incompatible evidence is never treated as an equivalent current result. Identical terminal provider failures are also blocked because executor/provider retry policy already owns low-level retries.

The Graph runtime does not pin a projection for the full request. If a later Graph result reports a different active version, prior-version Graph references remain available to final synthesis but become non-reusable, their covered Graph gaps reopen, and their action fingerprints no longer suppress a current-version retrieval. A result for a different gap/entity cannot mark that reopened gap unavailable.

Structured search arguments are rebuilt from the validated `TaskSpec`; model-supplied filters cannot replace them. Graph requests cannot expand the validated scope or depth. Provider payloads, prompts, model output, credentials, and hidden reasoning are excluded from metrics and Human Trace.

## Budgets and stopping

The request-level `AgentLoopBudget` is authoritative. Defaults are four Investigator turns, six total LLM calls, six total capability calls, two deepened entities, two technical failures, graph depth two, a 90-second adaptive deadline from `.env.example`, an 8,000-token Investigator target, and a 12,000-token hard input limit. One LLM call is reserved for final synthesis. LangGraph's recursion limit is only a safety backstop.

The loop stops on evidence sufficiency, goal completion, validated clarification, no useful/repeated action, deadline or turn exhaustion, LLM/tool/context budget exhaustion, technical-failure ceiling, or unavailable current evidence. Two consecutive deterministic no-progress observations stop with `no_useful_action`. A successful tool status alone is not progress: progress requires a new context identity or semantic fingerprint, newly covered gap, contradiction change, or newly authorized structured candidate. `FINISH` is accepted only when deterministic gap state permits it. Otherwise it is rejected and the bounded loop continues or ends safely.

Failures produce a bounded final answer with explicit limitations whenever synthesis remains possible. Invalid Investigator output consumes a turn and contributes to the technical-failure ceiling; it never bypasses validation. Intermediate decisions are not written to memory. Only the normal terminal memory-update stage persists eligible final continuity.

## Context boundaries

The Investigator receives a freshly rendered state each turn, never an accumulated model transcript. Mandatory sections are the typed task and request constraints, temporal/evidence mode, authorized entities and structured candidates, unresolved material gaps, remaining budget, relevant capability schemas, a compact evidence-reference index, and the latest observation delta. The delta contains only new/changed references, newly covered/remaining gaps, contradiction changes, rejected actions, progress state, and budget change.

Compaction is deterministic. It first removes older/non-material summaries and reduces memory and reference indexes while retaining latest/open-gap references; an aggressive pass further bounds non-authority metadata. Task authority, live/no-live policy, entity/candidate scope, unresolved gap IDs, budget, latest delta, and capability schemas are never arbitrarily string-truncated. The existing `TokenEstimator` records pre- and post-compaction estimates, tokens removed, reference/delta/schema counts, and remaining hard budget. If mandatory authority still exceeds the hard limit, the loop terminates with `context_budget_exhausted`.

The ledger retains at most 24 evidence references, at most two semantic states per context identity, 24 action fingerprints, 24 evidence fingerprints, 20 structured candidates, and eight recent deltas. The adaptive state is composed of serializable typed data, but its absolute monotonic request deadline is process-local. LangGraph checkpoint persistence and restart/resume are not enabled; the runtime does not claim restart-safe adaptive execution.

The final Synthesizer continues to use the existing evidence/context pipeline. It receives only adaptive execution metadata: orchestration mode, stop reason, turn count, unresolved-gap count, and whether a budget was exhausted.

## Observability

Safe workflow events cover selection, loop initialization, progress checks, decision request/receipt/invalidity, context budget and compaction, action validation/rejection/equivalence, tool observation, evidence-reference and delta changes, ledger updates, no-progress observations, and loop completion. Fields are allow-listed and bounded.

Prometheus records adaptive loop outcomes, turns, duration, action outcomes, equivalent calls blocked, evidence-reference changes, turns with/without material progress, stop reasons, budget-exhaustion categories, Investigator context size before/after compaction, estimated token savings, per-request logical LLM/capability calls, rejected premature finishes, rejected authority-invalid/malformed proposals, mode-grouped end-to-end latency, and deterministic review outcomes by orchestration mode. Existing LLM metrics and Product usage accounting report actual Investigator provider calls under `purpose=investigator`.

Human Trace adds the selected orchestration mode and a bounded `ADAPTIVE AGENT LOOP` section with aggregate budget/stop data and per-turn reference/gap deltas, skipped execution reason, progress, and context-token summaries. It contains no prompt, raw fingerprint, raw payload, arbitrary model output, or chain-of-thought.

## Phase4C compatibility and evaluation

Direct Phase4C structured search/aggregation and deterministic focal deepening remain the default behavior when the feature flag is off and remain available for simple work when it is on. Adaptive structured discovery reuses the same typed search contracts, treats returned assets as bounded candidates, and opens only validated post-search gaps.

Patch D provides deterministic replay and opt-in real-Investigator shadow replay over a versioned synthetic corpus. The release gate measures gap completion, tool precision, stopping, authority, calls, context size, usage, and latency where available. It does not use an evaluator LLM, mirror live production traffic, call live tools, or write memory. See [AUTONOMOUS_AGENT_EVALUATION.md](AUTONOMOUS_AGENT_EVALUATION.md).

Passing deterministic replay means `READY_FOR_MODEL_REPLAY`; passing configured model replay means `READY_FOR_STAGING`. Staging and canary evidence are still required before default enablement, so architecture completion does not change the default flag, enable checkpoints, or alter public chat/streaming contracts, capability authority, final synthesis, or memory persistence.
