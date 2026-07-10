---

name: soorin-copilot-runtime
description: Use for Soorin Copilot implementation, runtime diagnosis, repository hygiene, safe product API integration, logging, and focused smoke validation.
---------------------------------------------------------------------------------------------------------------------------------------------------------------

# Soorin Copilot Runtime

## Workspace

Active repo:

`/media/vxidalira/X1/Soorin/Asset_Management_Discovery_Platform/Project/Copilot`

Do not modify the reference repo unless explicitly requested.

## Before changes

Check:

```text
pwd
git branch --show-current
git status --short
active Python/venv
relevant settings and imports
running API/UI ports when needed
```

Inspect the real execution path before editing. Do not overwrite unrelated working-tree changes.

## Engineering rules

* Use environment-backed configuration and keep `app/.env` untracked.
* Never print or commit tokens, headers, raw provider responses, or private topology data.
* Use provider-neutral LLM and reusable product-client boundaries.
* Keep modules focused, typed where useful, and free of hidden cross-layer dependencies.
* Add concise docstrings/comments only for non-obvious contracts and trust boundaries.
* Add compact lifecycle logs to important application services:

```text
event=<name> key=value
```

Log state, counts, latency, provider/model, paths, and failures—not secrets or large payloads.

## Validation discipline

Use narrow checks in dependency order:

1. configuration and imports
2. compile
3. direct component smoke
4. API health and relevant endpoint
5. UI integration
6. final Git/secret/generated-file review

For reliable terminal diagnostics, prefer `API_RELOAD=false`.

Fix the earliest failing dependency only. Do not add broad frameworks, tests, files, or dependencies unless the task requires them.

## Product integration

For external product data:

* validate responses at the boundary;
* distinguish missing, invalid, and empty results;
* keep fetch, transformation, storage, query, context, and visualization separate;
* preserve source and freshness metadata;
* return compact evidence digests to Copilot-facing layers;
* document limitations such as observed communication versus actual routed reachability.

## Completion report

State compactly:

* inspected and changed files;
* preserved behavior;
* checks executed and results;
* security/config findings;
* unresolved risks;
* safest next step.

Do not commit unless explicitly requested.
