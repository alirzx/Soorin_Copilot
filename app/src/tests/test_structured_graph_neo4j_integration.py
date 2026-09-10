"""Isolated Neo4j Community coverage for structured retrieval through Phase 4B.3."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.config.settings import get_settings
from src.core.agent.contracts import TaskSpec
from src.core.agent.evidence import context_package_from_evidence
from src.core.agent.executor import CapabilityExecutor
from src.core.agent.plan_validator import PlanValidator
from src.core.agent.registry import build_capability_registry
from src.core.agent.reviewer import EvidenceReviewer
from src.core.agent.structured_evidence import expected_structured_query_identity
from src.core.agent.specialists import GraphAnalysisSpecialist
from src.core.agent.task_mapping import compile_direct_plan
from src.core.context.composer import ContextComposer
from src.core.context.models import EntityResolution
from src.core.context.providers.graph import GraphContextProvider
from src.core.context.synthesizer_prompt import SynthesizerPromptBuilder
from src.core.graph.enrichment_models import AssetEnrichmentMutation
from src.core.graph.neo4j import Neo4jDriver, Neo4jGraphRepository
from src.core.graph.service import GraphService
from src.core.graph.structured import (
    AssetAggregateRequest,
    AssetSearchFilters,
    AssetSearchRequest,
    StructuredQuerySpec,
)
from src.core.product_client import ProductAssetDetectionOverview
from src.core.product_client.schemas import TopologyConnectionRecord


pytestmark = pytest.mark.skipif(
    os.getenv("SOORIN_RUN_NEO4J_INTEGRATION") != "1",
    reason="set SOORIN_RUN_NEO4J_INTEGRATION=1 to run against isolated Neo4j Community",
)


ASSETS = (
    {
        "ip": "10.20.0.1", "assetName": "DC-LOW", "status": "CONFIRMED",
        "suggestedType": "Server", "modelConfidence": 0.65,
        "mappingConfidence": 0.70, "unknownScore": 0.35, "vendor": "VMware",
        "product": "Active Directory", "role": "Domain Controller",
        "roles": ["Domain Controller", "LDAP Server"], "tag": "identity",
        "subTag": "directory", "classificationSummary": "Low-confidence DC.",
        "lastDetectionAt": "2026-09-09T08:00:00Z",
    },
    {
        "ip": "10.20.0.2", "assetName": "DC-HIGH", "status": "CONFIRMED",
        "suggestedType": "Server", "modelConfidence": 0.95,
        "mappingConfidence": 0.92, "unknownScore": 0.05, "vendor": "Microsoft",
        "product": "Active Directory", "role": "Domain Controller",
        "roles": ["Domain Controller", "DNS Server"], "tag": "identity",
        "subTag": "directory", "classificationSummary": "High-confidence DC.",
        "lastDetectionAt": "2026-09-10T08:00:00Z",
    },
    {
        "ip": "10.20.0.3", "assetName": "VM-WEB", "status": "REVIEW",
        "suggestedType": "Server", "modelConfidence": 0.72,
        "mappingConfidence": 0.60, "unknownScore": 0.28, "vendor": "VMware",
        "product": "Nginx", "role": "Web Server", "roles": ["Web Server"],
        "tag": "web", "subTag": "frontend", "classificationSummary": "Web node.",
        "lastDetectionAt": "2026-09-08T08:00:00Z",
    },
    {
        "ip": "10.20.0.4", "assetName": "LDAP-01", "status": "REVIEW",
        "suggestedType": "Server", "modelConfidence": 0.55,
        "mappingConfidence": 0.58, "unknownScore": 0.45, "vendor": "OpenLDAP",
        "product": "OpenLDAP", "role": "Directory Server", "roles": ["LDAP Server"],
        "tag": "identity", "subTag": "ldap", "classificationSummary": "LDAP node.",
        "lastDetectionAt": "2026-09-07T08:00:00Z",
    },
    {
        "ip": "10.20.0.5", "assetName": "CLIENT-01", "status": "CONFIRMED",
        "suggestedType": "Workstation", "modelConfidence": 0.80,
        "mappingConfidence": 0.76, "unknownScore": 0.20, "vendor": "VMware",
        "product": "Horizon", "role": "Workstation", "roles": ["Client"],
        "tag": "endpoint", "subTag": "desktop", "classificationSummary": "Client.",
        "lastDetectionAt": "2026-09-06T08:00:00Z",
    },
)


@pytest.fixture(scope="module")
def structured_repository() -> Neo4jGraphRepository:
    settings = get_settings()
    driver = Neo4jDriver(settings)
    repository = Neo4jGraphRepository(driver, settings)
    repository.bootstrap_schema()
    repository.sync_snapshot(
        [
            TopologyConnectionRecord("10.20.0.1", "10.20.0.2"),
            TopologyConnectionRecord("10.20.0.2", "10.20.0.3"),
            TopologyConnectionRecord("10.20.0.3", "10.20.0.4"),
            TopologyConnectionRecord("10.20.0.4", "10.20.0.5"),
        ],
        "structured-active-v2",
    )
    now = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
    repository.apply_enrichment_batch(
        [
            AssetEnrichmentMutation.success(
                ProductAssetDetectionOverview.from_payload(payload, expected_ip=payload["ip"]),
                attempted_at=now,
                freshness_seconds=259200,
            )
            for payload in ASSETS
        ]
    )
    with driver.session() as session:
        session.run(
            "MATCH (a:Asset {graph_version: $version, graph_key: '10.20.0.2'}) "
            "SET a.enrichment_status = 'stale'",
            version="structured-active-v2",
        ).consume()
        session.run(
            "CREATE (:Asset {graph_key: '10.20.0.1', ip: '10.20.0.1', "
            "graph_version: 'structured-historical-v1', role: 'Historical Role'})"
        ).consume()
    yield repository
    driver.close()


def _ips(repository: Neo4jGraphRepository, **filters: object) -> list[str]:
    result = repository.search_assets(
        AssetSearchRequest(filters=AssetSearchFilters(**filters), limit=50)
    )
    return [row.ip for row in result.rows]


def test_exact_and_structured_filter_semantics(structured_repository: Neo4jGraphRepository) -> None:
    repository = structured_repository
    assert _ips(repository, ip="10.20.0.1") == ["10.20.0.1"]
    assert _ips(repository, asset_name="DC-LOW") == ["10.20.0.1"]
    assert _ips(repository, role="Domain Controller") == ["10.20.0.1", "10.20.0.2"]
    assert _ips(repository, status="CONFIRMED") == ["10.20.0.1", "10.20.0.2", "10.20.0.5"]
    assert _ips(repository, product="Active Directory") == ["10.20.0.1", "10.20.0.2"]
    assert _ips(repository, vendor="VMware") == ["10.20.0.1", "10.20.0.3", "10.20.0.5"]
    assert _ips(repository, roles="LDAP Server") == ["10.20.0.1", "10.20.0.4"]
    assert _ips(repository, model_confidence_max=0.65) == ["10.20.0.1", "10.20.0.4"]
    assert _ips(repository, unknown_score_min=0.35) == ["10.20.0.1", "10.20.0.4"]
    assert _ips(repository, enrichment_status="stale") == ["10.20.0.2"]
    assert _ips(repository, role="Domain Controller", product="Active Directory") == ["10.20.0.1", "10.20.0.2"]
    assert _ips(repository, status="CONFIRMED", model_confidence_max=0.7) == ["10.20.0.1"]
    assert _ips(repository, product="does-not-exist") == []


def test_active_version_excludes_duplicate_historical_asset(structured_repository: Neo4jGraphRepository) -> None:
    result = structured_repository.search_assets(
        AssetSearchRequest(filters=AssetSearchFilters(ip="10.20.0.1"))
    )
    assert result.active_graph_version == "structured-active-v2"
    assert [(row.graph_version, row.role) for row in result.rows] == [
        ("structured-active-v2", "Domain Controller")
    ]


def test_bounded_keyset_pagination_and_safe_sort(structured_repository: Neo4jGraphRepository) -> None:
    cursor = None
    collected: list[str] = []
    while True:
        page = structured_repository.search_assets(
            AssetSearchRequest(limit=2, cursor=cursor)
        )
        collected.extend(row.graph_key for row in page.rows)
        if not page.truncated:
            break
        assert page.next_cursor and page.next_cursor != cursor
        cursor = page.next_cursor
    assert collected == sorted(payload["ip"] for payload in ASSETS)
    assert len(collected) == len(set(collected))
    ranked = structured_repository.search_assets(
        AssetSearchRequest(sort="model_confidence", direction="desc", limit=3)
    )
    assert [row.model_confidence for row in ranked.rows] == [0.95, 0.80, 0.72]


def test_count_and_group_count_execute_in_neo4j(structured_repository: Neo4jGraphRepository) -> None:
    repository = structured_repository
    assert repository.aggregate_assets(AssetAggregateRequest()).count == 5
    assert repository.aggregate_assets(
        AssetAggregateRequest(filters=AssetSearchFilters(role="Domain Controller"))
    ).count == 2
    assert repository.aggregate_assets(
        AssetAggregateRequest(filters=AssetSearchFilters(product="Active Directory"))
    ).count == 2
    assert repository.aggregate_assets(
        AssetAggregateRequest(filters=AssetSearchFilters(model_confidence_max=0.7))
    ).count == 2
    grouped = repository.aggregate_assets(
        AssetAggregateRequest(operation="group_count", group_by="status")
    )
    assert grouped.count == 5
    assert {(group.value, group.count) for group in grouped.groups} == {
        ("CONFIRMED", 3), ("REVIEW", 2)
    }


def test_agent_capability_path_reaches_active_structured_projection(
    structured_repository: Neo4jGraphRepository,
) -> None:
    settings = get_settings()
    service = object.__new__(GraphService)
    service.settings = settings
    service.repository = structured_repository
    provider = object.__new__(GraphContextProvider)
    provider.settings = settings
    provider.graph_service = service
    unused = SimpleNamespace(settings=settings)
    registry = build_capability_registry(
        asset_profile_provider=unused,
        detection_provider=unused,
        graph_provider=provider,
        knowledge_service=unused,
    )
    query = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"status": "CONFIRMED", "role": "Domain Controller"},
        "sort": "model_confidence",
        "direction": "asc",
        "limit": 10,
    })
    task = TaskSpec(
        request="List confirmed Domain Controllers",
        intent="asset_search",
        scope="none",
        direction="none",
        entities=(),
        required_capabilities=("graph.search_assets",),
        structured_query=query,
        workflow_mode="direct",
    )
    plan = PlanValidator(registry).validate(compile_direct_plan(task))
    output = GraphAnalysisSpecialist(CapabilityExecutor(registry)).run({
        "request_id": "phase4b2-neo4j",
        "session_id": "phase4b2-neo4j",
        "workflow_id": "phase4b2-neo4j",
        "execution_plan": plan,
    })
    result = output["tool_results"][0]
    assert result.status == "ok"
    assert result.entities == ()
    assert result.total_count == result.included_count == 2
    assert [row["ip"] for row in result.raw_payload["rows"]] == [
        "10.20.0.1",
        "10.20.0.2",
    ]
    assert all(row["graph_version"] == "structured-active-v2" for row in result.raw_payload["rows"])
    assert result.evidence_type == "graph_asset_search"
    assert result.structured_asset_set is not None
    assert result.structured_asset_set.active_graph_version == "structured-active-v2"
    expected_identity = expected_structured_query_identity(query, result.structured_asset_set)
    assert result.structured_asset_set.query_identity == expected_identity, result.structured_asset_set

    reviewer = EvidenceReviewer()
    review = reviewer.review(task, [result])
    assert review.outcome == "sufficient", review
    pack = reviewer.build_pack(task, [result], plan=plan, review=review)
    package = context_package_from_evidence(pack, EntityResolution(status="none"))
    composer = ContextComposer(settings)
    dynamic_context = composer.compose(
        package,
        base_input_tokens=200,
        reserved_output_tokens=1024,
        request_id="phase4b3-neo4j",
    )
    builder = SynthesizerPromptBuilder()
    synth_context = builder.build_context(task, (result,), review=review)
    prompt, modules = builder.render_contract(synth_context)
    assert "SOORIN_STRUCTURED_ASSET_SET_CONTEXT_JSON" in dynamic_context
    assert '"matched_total":2' in dynamic_context
    assert "task.asset_search" in modules
    assert "enrichment-derived organizational projection" in prompt
