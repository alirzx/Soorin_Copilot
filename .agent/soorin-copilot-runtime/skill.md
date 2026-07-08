---
name: soorin-copilot-runtime
description: Use for Soorin Copilot runtime checks, provider debugging, repo hygiene, smoke tests, migration safety, and safe development workflow.
---

# Soorin Copilot Runtime

## Active project

Use this repo:

- /media/vxidalira/X1/Soorin/Asset_Management_Discovery_Platform/Project/Copilot

Reference repo:

- /media/vxidalira/X1/Soorin/Asset_Management_Discovery_Platform/Project/Asset_management_ML_Engine

Do not edit the reference repo unless explicitly requested.

## Runtime discipline

Before debugging or editing, check:

- pwd
- git branch --show-current
- git status --short
- python path/version
- active env file/template
- running API/UI ports if relevant

Use the project venv when available.

Never print secrets or full `.env` files.

## Provider rules

LLM access must go through a provider-neutral client.

Current target provider:

- GLM-5.2 through API provider/Arvan-style chat completions

Provider code should normalize:

- text
- model
- provider
- usage
- finish_reason
- latency
- fallback state
- error state

Do not expose raw reasoning content, API keys, headers, or full raw provider responses.

## Smoke order

Use narrow smoke tests:

1. config loads
2. provider direct chat works
3. `/copilot/chat` works
4. conversation persistence works
5. Streamlit chat works
6. context provider works
7. RAG works
8. graph context works
9. GraphRAG works

Fix only the earliest failing dependency.

Do not run broad tests before the baseline is stable.

## Migration safety

When reusing old repo code:

- inspect first
- copy the smallest useful unit
- remove ML-specific assumptions
- add focused smoke/test
- keep public contracts simple
- document the copied source and changes

Do not copy old trainer, model artifacts, prediction pipeline, fusion, XAI, or governance logic into the new repo unless explicitly redesigned.

## Output expectations

For any task, report compactly:

- what was inspected
- what changed
- what was not touched
- how it was tested
- next safest step