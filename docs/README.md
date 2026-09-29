# Soorin Copilot Documentation

This directory contains the operational and engineering documentation for Soorin Cyber Copilot.

## Canonical Current Documentation

These documents describe the current runtime and should be treated as the primary references for implementation, deployment, integration, and operations:

- [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md) — end-to-end runtime architecture, request workflow, evidence authority, graph, memory, LLM roles, and service boundaries.
- [DEPLOYMENT.md](DEPLOYMENT.md) — Docker/Compose deployment, persistent storage, Hugging Face cache, Neo4j, observability, Makefile preflight, and server rollout.
- [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md) — runtime configuration contract and environment-variable reference.
- [FRONTEND_BACKEND_COPILOT_INTEGRATION.md](FRONTEND_BACKEND_COPILOT_INTEGRATION.md) — frontend/API integration contract, authentication, conversation IDs, UI-selected asset context, and streaming behavior.
- [NEO4J_GRAPH_ENRICHMENT_GRAPHRAG.md](NEO4J_GRAPH_ENRICHMENT_GRAPHRAG.md) — Neo4j projection, synchronization, enrichment, structured graph retrieval, and graph-aware investigation.
- [OBSERVABILITY.md](OBSERVABILITY.md) — application logging, Prometheus, Loki, Alloy, Grafana, metrics authentication, and evidence diagnostics.
- [AUTONOMOUS_AGENT_WORKFLOW.md](AUTONOMOUS_AGENT_WORKFLOW.md) — feature-flagged bounded adaptive investigation, contracts, budgets, safety boundaries, and rollout behavior.

## Architecture and Design References

The following documents contain detailed design rationale or subsystem-specific background that remains useful when working on the current implementation:

- [MEMORY_CONTEXT_UPGRADE_DESIGN.md](MEMORY_CONTEXT_UPGRADE_DESIGN.md)
- [INTERNAL_EVIDENCE_TO_MODEL_CONTEXT_AUDIT.md](INTERNAL_EVIDENCE_TO_MODEL_CONTEXT_AUDIT.md)

## Historical Engineering Snapshots

The documents below record earlier phase implementation, validation, or audit state. They are retained for engineering history and regression context, but the canonical current documents above take precedence whenever descriptions differ:

- [PHASE4_STRUCTURED_GRAPH_ROUTING.md](PHASE4_STRUCTURED_GRAPH_ROUTING.md)
- [PHASE4C_EXACT_SEARCH_FINALIZATION.md](PHASE4C_EXACT_SEARCH_FINALIZATION.md)
- [POST_CODEX_PHASE4_STATUS_2026-09-13.md](POST_CODEX_PHASE4_STATUS_2026-09-13.md)
- [MEMORY_WORKFLOW_CURRENT_DEV_AUDIT.md](MEMORY_WORKFLOW_CURRENT_DEV_AUDIT.md)

## Runtime Authority

When documentation and runtime configuration differ, use this order to resolve the active contract:

1. application and Compose code;
2. tracked `.env.example` schema;
3. canonical current documentation in this index;
4. historical design/audit documents.

Private `.env` values are deployment-specific and must not be committed.
