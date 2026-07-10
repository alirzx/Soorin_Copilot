---

name: soorin-copilot-architecture
description: Use for Soorin Copilot architecture, module boundaries, product integration, context engineering, graph/RAG design, and selective migration from the reference repo.
---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

# Soorin Copilot Architecture

## Direction

This is a Copilot-first cybersecurity product for SOC/NOC analysis, asset intelligence, network topology, investigations, and grounded technical Q&A.

Active repo:

`/media/vxidalira/X1/Soorin/Asset_Management_Discovery_Platform/Project/Copilot`

Reference repo:

`/media/vxidalira/X1/Soorin/Asset_Management_Discovery_Platform/Project/Asset_management_ML_Engine`

Treat the reference repo as a source of reviewed components and lessons, not as the target architecture.

## Core principles

* The Copilot is the orchestration and analyst-facing layer.
* Real product endpoints and deterministic services own operational evidence.
* LLM outputs are analytical and advisory; never silently mutate product truth.
* Normalize all sources into compact, typed context packages.
* Prefer context providers over direct dependencies inside `CopilotService`.
* Preserve provenance, limitations, timestamps, and uncertainty.
* Keep vector knowledge retrieval separate from graph relationship retrieval.
* Use bounded, auditable agentic workflows only after deterministic context flows are stable.

## Target flow

```text
User + UI state + conversation
→ entity/intent routing
→ selected context providers
→ compact context package
→ prompt/message builder
→ provider-neutral LLM
→ grounded response
```

Expected providers include asset, detection, topology/graph, Vector RAG, threat intelligence, and future analyst-workflow services.

## Design rules

* Keep API, product-client, graph, RAG, context, Copilot, LLM, memory, and UI responsibilities separate.
* Product HTTP/auth logic belongs in `core/product_client`.
* Graph services return deterministic queries and compact digests; visualization stays in the web layer.
* Do not send raw full graphs or unbounded endpoint payloads to an LLM.
* Avoid hardcoded paths, ports, credentials, endpoint URLs, and UI counts.
* Extend stable interfaces instead of redesigning the baseline chat path.

## Migration rule

Before reusing reference code, classify it as:

* `reuse_now`
* `adapt`
* `redesign`
* `do_not_migrate`

Copy only the smallest reviewed unit. Remove obsolete ML-first, prediction-centered, or monolithic assumptions.
