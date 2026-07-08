---
name: soorin-copilot-architecture
description: Use for Soorin Copilot architecture, module design, migration planning, and keeping development aligned with the Copilot-first product direction.
---

# Soorin Copilot Architecture

## Project identity

This repo is the new Copilot-first Soorin project.

Primary repo:

- /media/vxidalira/X1/Soorin/Asset_Management_Discovery_Platform/Project/Copilot

Reference repo:

- /media/vxidalira/X1/Soorin/Asset_Management_Discovery_Platform/Project/Asset_management_ML_Engine

The reference repo is a source of reusable code and design ideas, not the architecture target.

## Product goal

Build one unified cybersecurity Copilot for SOC/NOC asset intelligence.

The Copilot should support:

- normal chatbot Q&A
- grounded Q&A about a selected asset/IP/node
- retrieval from engineering/security knowledge
- graph-aware reasoning over network topology
- evidence-aware responses from product context
- future analyst workflows, reports, audit, and feedback

Start simple. Add intelligence layers gradually.

## Architecture principle

Copilot is the product spine.

Context providers feed the Copilot.

The LLM must not own product truth. It explains, reasons, summarizes, compares, and assists using provided context.

## Development order

Prefer this order:

1. baseline API chatbot
2. provider abstraction
3. conversation memory
4. Streamlit chat UI
5. asset/context pack
6. context router
7. vector RAG
8. graph/topology context
9. GraphRAG
10. audit/feedback
11. reports and advanced workflows

Do not introduce RAG, graph, agents, or complex orchestration before the baseline chatbot works.

## Suggested module boundaries

Keep modules small and replaceable:

- api
- core/copilot
- core/llm
- core/memory
- core/context
- core/rag
- core/graph
- core/audit
- core/runtime
- web

Avoid hidden dependencies between layers.

## Migration rule

Before copying anything from the reference repo, classify it as:

- reuse_now
- reuse_later
- redesign
- do_not_migrate

Copy only small, reviewed modules. Do not bulk-copy old pipeline code.

## Avoid

- old ML-first workflow
- prediction-centered contracts
- cluster/fusion/governance assumptions unless redesigned
- provider-specific business logic
- hard-coded local paths
- exposing secrets or raw provider responses
- large unreviewed migrations