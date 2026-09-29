# Adaptive Investigator Evaluation and Rollout

## Purpose

Patch D evaluates the bounded adaptive Investigator without creating a second live production workflow. The release gate is deterministic, synthetic, versioned, and independent of customer systems. It measures action selection, required-evidence completion, stopping, authority enforcement, context size, calls, and readiness while leaving `SOORIN_ADAPTIVE_AGENT_ENABLED=false` by default.

The evaluator lives in `app/src/core/agent/evaluation.py`. It has no `CapabilityExecutor`, Product client, Neo4j repository, Qdrant service, conversation repository, or long-term-memory writer. Production request routing does not import or invoke it.

## Corpus and policy

The tracked corpus is `app/evals/adaptive_agent/scenarios.jsonl` with corpus version `adaptive-agent-eval-v1`. It contains only synthetic RFC 5737 entities. Each scenario declares a typed task, request constraints, evidence gaps, policy-level expectations, bounded scripted decisions for deterministic replay, and synthetic results that can be released only after a canonical action passes both production validators.

The corpus covers direct and fixed parity, memory-only and no-live behavior, current-versus-historical authority, Product/Detection/Graph/Knowledge evidence, structured discovery and focal deepening, continuity, provider failures, malformed decisions, authority attacks, premature stopping, equivalence, no-progress and independent budgets, injection text, and an in-request Graph projection-version switch. Expectations allow valid policy outcomes instead of requiring one arbitrary tool order.

Release thresholds are versioned separately in `app/evals/adaptive_agent/thresholds.json`. They are evaluation policy, not runtime environment variables.

## Replay modes

### Deterministic replay

`replay` is the CI gate. Scripted decisions still pass through the real `Investigator.parse`, `AgentActionValidator`, `PlanValidator`, canonical fingerprint/equivalence logic, `EvidenceReference`, ledger update, observation, and deterministic progress gate. Validated actions resolve synthetic `ToolResult` fixtures; no provider or LLM is called and no memory is written.

```bash
make agent-eval

# Equivalent explicit command
PYTHONPATH=app .venv/bin/python app/scripts/evaluate_adaptive_agent.py \
  --mode replay \
  --scenarios app/evals/adaptive_agent/scenarios.jsonl \
  --thresholds app/evals/adaptive_agent/thresholds.json \
  --output adaptive-agent-eval-report.json
```

Exit status `0` means every configured hard and quality gate passed. Gate failure returns `1`; invalid or unavailable configuration returns `2`.

### Model-backed shadow replay

`model-replay` is explicit and consumes Investigator tokens. It uses the configured real Investigator prompt, context builder, transport, parser, production validators, ledger, equivalence, and stopping logic. Only the Investigator LLM is allowed to be external. Tool outcomes remain synthetic and are selected after validated canonical action identity; Product, Neo4j, Qdrant, final Synthesizer, and memory persistence are not invoked.

```bash
make agent-eval-model

# Equivalent explicit command
PYTHONPATH=app .venv/bin/python app/scripts/evaluate_adaptive_agent.py \
  --mode model-replay \
  --scenarios app/evals/adaptive_agent/scenarios.jsonl \
  --thresholds app/evals/adaptive_agent/thresholds.json \
  --output adaptive-agent-model-replay-report.json
```

If the Investigator deployment is not explicitly configured, the command records `status=unavailable`, reports `NOT_READY`, makes no model call, and exits `2`. Model replay is never part of ordinary pytest or CI.

## Measurements

The report includes:

- scenario success and required-gap completion/failure;
- tool-selection precision and coverage, unnecessary executed calls, duplicate provider calls, and equivalent-action suppression;
- authority violations and premature `FINISH`, separated into proposed and accepted rates;
- clarification accuracy, no-useful-action, budget exhaustion, technical failures, turns, and capability calls;
- estimated compact-context input size and savings in replay;
- provider input/output tokens and Investigator latency when provider usage is available in model replay;
- deterministic review-outcome distribution and category summaries;
- comparable legacy baseline evidence coverage, tool calls, and logical LLM calls for selected adaptive scenarios.

An invalid proposal is not counted as an executed false-positive tool call. Required unavoidable failures pass only when the scenario expects `answer_with_limitations` and the final gap semantics match.

## Readiness gates

Hard gates require zero accepted authority violations, accepted premature finishes, forbidden live execution in memory/no-live cases, Graph authority bypasses, accepted structured-query mutation, raw evidence/context leakage, raw reasoning/report leakage, direct-to-adaptive misrouting, duplicate equivalent provider execution, and intermediate memory writes. Any hard-gate failure yields `NOT_READY`.

Initial synthetic quality gates are:

- scenario success at least 95%;
- required-gap completion at least 95%;
- tool-selection precision at least 90%;
- unnecessary executed calls at most 10%;
- duplicate provider-call rate, accepted premature finish, accepted authority violation, and direct adaptive entry exactly zero;
- budget exhaustion on normal solvable scenarios at most 5%.

Wall-clock latency is reported but is not a CI gate. Missing provider token usage remains `null`; the evaluator never invents usage.

One centralized readiness calculation maps completed passing deterministic replay to `READY_FOR_MODEL_REPLAY` and completed passing real model replay to `READY_FOR_STAGING`. Tests or local model replay cannot grant canary/default readiness.

## Report identity and privacy

JSON reports record the evaluation schema, Git commit SHA, mode, bounded deployment/model names, whether configured sampling controls are supported, SHA-256 of the tracked Investigator prompt, corpus/threshold versions, aggregate/category metrics, gate results, failed scenario IDs/reason codes, and readiness. `--include-results` optionally adds bounded structural decisions and capability names.

Reports exclude prompts, endpoints, credentials, raw model responses, assessment text, raw `ToolResult` payloads, evidence text, memory statements, and real identifiers. The injection fixture proves untrusted limitation text cannot enter Investigator context or the report.

## Deployment policy

Rollout is deployment-scoped rather than random per-request routing:

1. Production remains disabled.
2. Deterministic replay must pass.
3. The configured Investigator must pass model-backed shadow replay.
4. A dedicated staging deployment runs with `SOORIN_ADAPTIVE_AGENT_ENABLED=true` and is compared with the fixed/direct baseline.
5. A limited organization/deployment canary is enabled only after explicit review of staging telemetry.
6. Canary telemetry must meet the same safety invariants and the operational thresholds selected by the release owner.
7. Default enablement requires an explicit review decision backed by model-replay, staging, and production-canary evidence.

The immediate rollback/kill switch is:

```env
SOORIN_ADAPTIVE_AGENT_ENABLED=false
```

Disabling the flag avoids Investigator construction and adaptive context/ledger execution on legacy paths. It does not require a distributed rollback service and does not alter stored conversation or memory formats.

## Operational review

Before advancing a stage, review mode-grouped request latency, LLM and capability calls per adaptive request, stop/budget outcomes, rejected premature finishes, rejected authority-invalid and malformed proposals, equivalent calls blocked, context savings, and evidence-review outcomes. The canonical Grafana dashboard includes an Adaptive Investigator readiness row, and [OBSERVABILITY.md](OBSERVABILITY.md) lists the underlying metrics.

The architecture can be implementation-complete while rollout evidence remains incomplete. In that state the professional decision is to keep the default false and continue with the next evidence stage.
