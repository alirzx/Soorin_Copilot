"""Focused offline tests for route-aware exhaustive graph context allocation."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from typing import Any

from src.config.settings import get_settings
from src.core.context.composer import ContextComposer
from src.core.context.entities import EntityResolver
from src.core.context.models import (
    AssetProfileProviderResult,
    CopilotContextPackage,
    DetectionProviderResult,
    GraphProviderResult,
    ProviderProvenance,
    approx_tokens,
)
from src.core.memory.store import MemoryStore
from src.core.llm.token_estimator import TokenEstimator


TARGET = "192.168.30.115"


def settings(**overrides: Any):
    values = {
        "llm_context_window_tokens": 32768,
        "llm_reserved_output_tokens": 12288,
        "llm_context_safety_margin_tokens": 2048,
        "synthesizer_brief_output_tokens": 1536,
        "synthesizer_standard_output_tokens": 4096,
        "synthesizer_deep_output_tokens": 6144,
        "graph_max_context_tokens": 8000,
        "graph_context_max_enumerated_nodes": 500,
        "graph_context_max_enumerated_edges": 500,
        "graph_full_enumeration_max_peers": 500,
    }
    values.update(overrides)
    return replace(get_settings(), **values)


def product_result(provider: str, *, large: bool = False):
    payload: dict[str, Any]
    if provider == "asset_profile":
        payload = {
            "id": f"asset-{TARGET}",
            "ip_address": TARGET,
            "hostname": "fixture-host",
            "classification": {"role": "server", "confidence": 0.91},
            "identity": {"services": ["https", "ssh"], "domain_joined": True},
        }
        if large:
            payload["verbose_history"] = [
                {"event": index, "description": "profile detail " * 30}
                for index in range(300)
            ]
        result_type = AssetProfileProviderResult
    else:
        payload = {
            "ip": TARGET,
            "assetFound": True,
            "classification": {"role": "server", "confidence": 0.91},
            "matchedRules": [
                {"id": index, "evidence": [f"signal-{index}"]}
                for index in range(8 if not large else 500)
            ],
            "conflicts": [],
        }
        result_type = DetectionProviderResult
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return result_type(
        provider=provider,
        status="available",
        ip=TARGET,
        raw_payload=payload,
        serialized_json=serialized,
        provenance=ProviderProvenance(source=f"fixture_{provider}", status="available"),
        raw_payload_present=True,
        full_payload_fetched=True,
        raw_json_chars=len(serialized),
        raw_json_approx_tokens=approx_tokens(serialized),
        raw_top_level_key_count=len(payload),
    )


def rich_projected_detection(*, raw_payload_present: bool = True) -> DetectionProviderResult:
    result = product_result("detection", large=True)
    rich = {
        "provider": "detection",
        "views": {"full": result.raw_payload},
        "projection_metadata": {
            "source_payload_complete": True,
            "selected_views": ["full"],
            "normal_compaction": True,
        },
    }
    serialized = json.dumps(rich, separators=(",", ":"), sort_keys=True)
    return replace(
        result,
        raw_payload=result.raw_payload if raw_payload_present else None,
        raw_payload_present=raw_payload_present,
        serialized_json=serialized,
    )


def exhaustive_graph() -> GraphProviderResult:
    peers = [f"10.0.{index // 254}.{index % 254 + 1}" for index in range(254)]
    nodes = [
        {
            "id": TARGET,
            "hop": 0,
            "subnet": "192.168.30.0/24",
            "inbound": False,
            "outbound": False,
            "bidirectional": False,
        }
    ]
    nodes.extend(
        {
            "id": peer,
            "hop": 1,
            "subnet": "10.0.0.0/8",
            "inbound": index < 120 or index >= 236,
            "outbound": index >= 120,
            "bidirectional": index >= 236,
        }
        for index, peer in enumerate(peers)
    )
    edges = [{"source": peer, "target": TARGET, "weight": 1} for peer in peers[:120]]
    edges.extend({"source": TARGET, "target": peer, "weight": 1} for peer in peers[120:])
    edges.extend({"source": peer, "target": TARGET, "weight": 1} for peer in peers[236:254])
    context = {
        "target_ip": TARGET,
        "target_ips": [TARGET],
        "node_found": True,
        "scope": "full_neighbors",
        "requested_scope": "full_neighbors",
        "direction": "both",
        "depth": 1,
        "inbound_total": 138,
        "inbound_retrieved": 138,
        "outbound_total": 134,
        "outbound_retrieved": 134,
        "bidirectional_total": 18,
        "bidirectional_retrieved": 18,
        "candidate_node_count": 255,
        "retrieved_node_count": 255,
        "returned_node_count": 255,
        "candidate_edge_count": 272,
        "retrieved_edge_count": 272,
        "returned_edge_count": 272,
        "nodes": nodes,
        "edges": edges,
        "retrieval_complete": True,
        "retrieval_truncated": False,
        "retrieval_truncation_reason": None,
        "requested_scope_complete": True,
        "complete_for_user_request": True,
        "exhaustive_connections_requested": True,
        "limitations": [],
    }
    return GraphProviderResult(
        provider="graph",
        status="available",
        context=context,
        provenance=ProviderProvenance(source="fixture_graph", status="available"),
    )


def package(*, large_products: bool = False) -> CopilotContextPackage:
    return CopilotContextPackage(
        entities=EntityResolver().resolve(TARGET),
        graph=exhaustive_graph(),
        detections=[product_result("detection", large=large_products)],
        asset_profiles=[product_result("asset_profile", large=large_products)],
    )


def manifest(composer: ContextComposer) -> dict[str, Any]:
    return json.loads(composer.last_parts["status"].split("\n", 1)[1].rsplit("\n", 1)[0])


class ExhaustiveGraphBudgetTests(unittest.TestCase):
    def test_large_complete_graph_is_compacted_instead_of_dropped(self) -> None:
        composer = ContextComposer(settings())
        context_package = package()
        text = composer.compose(context_package, base_input_tokens=5500, request_id="budget-large")

        self.assertTrue(composer.last_parts["graph"])
        self.assertEqual(composer.last_inclusion["graph"], (True, None))
        self.assertEqual(composer.last_representation["graph"], "full_neighbors_summary")
        self.assertLessEqual(approx_tokens(composer.last_parts["graph"]), 3000)
        self.assertLessEqual(approx_tokens(text), 12932)

    def test_exact_totals_and_complete_peer_edge_inclusion_are_preserved(self) -> None:
        composer = ContextComposer(settings())
        context_package = package()
        composer.compose(context_package, base_input_tokens=5500)
        graph_coverage = manifest(composer)["provider_coverage"]["graph"]
        product_coverage = manifest(composer)["provider_coverage"]

        self.assertEqual(graph_coverage["returned_node_count"], 255)
        self.assertLess(graph_coverage["included_node_count"], 255)
        self.assertEqual(graph_coverage["returned_edge_count"], 272)
        self.assertEqual(graph_coverage["included_edge_count"], 0)
        self.assertTrue(graph_coverage["retrieval_complete"])
        self.assertFalse(graph_coverage["serialized_context_complete_for_retrieved_subset"])
        self.assertFalse(graph_coverage["model_input_graph_complete"])
        self.assertFalse(graph_coverage["complete_for_user_request"])
        self.assertIn('"omitted_peer_count"', composer.last_parts["graph"])
        self.assertIn('"continuation_guidance"', composer.last_parts["graph"])
        self.assertIn("[ASSET_PROFILE_CONTEXT_JSON", composer.last_parts["asset_profile"])
        self.assertIn("[ASSET_DETECTION_CONTEXT_JSON", composer.last_parts["detection"])
        self.assertTrue(product_coverage["asset_profile"]["payload_complete"])
        self.assertTrue(product_coverage["asset_detection"]["payload_complete"])

    def test_oversized_complete_product_payloads_use_compact_views_without_starving_graph(self) -> None:
        composer = ContextComposer(settings())
        composer.compose(package(large_products=True), base_input_tokens=5500)
        coverage = manifest(composer)["provider_coverage"]
        profile_entity = coverage["asset_profile"]["entities"][TARGET]
        detection_entity = coverage["asset_detection"]["entities"][TARGET]

        self.assertIn("[ASSET_PROFILE_CONTEXT_JSON", composer.last_parts["asset_profile"])
        self.assertIn("[ASSET_DETECTION_CONTEXT_JSON", composer.last_parts["detection"])
        self.assertTrue(composer.last_parts["graph"])
        self.assertEqual(profile_entity["representation"], "projected")
        self.assertEqual(detection_entity["representation"], "projected")
        self.assertTrue(coverage["asset_profile"]["payload_complete"])
        self.assertTrue(coverage["asset_detection"]["payload_complete"])
        self.assertTrue(profile_entity["source_payload_complete"])
        self.assertFalse(profile_entity["projection_truncated"])
        self.assertGreater(profile_entity["projection_omitted_count"], 0)
        self.assertFalse(composer.required_context_missing)

    def test_rich_detection_over_soft_cap_uses_mandatory_bounded_projection(self) -> None:
        composer = ContextComposer(settings())
        context_package = replace(
            package(),
            detections=[rich_projected_detection()],
        )

        text = composer.compose(context_package, base_input_tokens=5500)
        detection = manifest(composer)["provider_coverage"]["asset_detection"]["entities"][TARGET]

        self.assertGreater(approx_tokens(rich_projected_detection().serialized_json), 2400)
        self.assertIn("[ASSET_DETECTION_CONTEXT_JSON", composer.last_parts["detection"])
        self.assertEqual(detection["representation"], "bounded_minimum")
        self.assertTrue(detection["payload_included"])
        self.assertTrue(detection["projection_truncated"])
        self.assertFalse(detection["payload_complete"])
        self.assertFalse(composer.required_context_missing)
        self.assertLessEqual(approx_tokens(text), composer.last_budget["max_dynamic_tokens"])

    def test_deduplicated_support_does_not_count_as_detection_provider_inclusion(self) -> None:
        composer = ContextComposer(settings())
        context_package = replace(
            package(),
            detections=[rich_projected_detection(raw_payload_present=False)],
        )

        composer.compose(context_package, base_input_tokens=5500)

        self.assertIn("[SOORIN_DEDUPLICATED_FACT_SUPPORT]", composer.last_parts["detection"])
        self.assertFalse(composer.last_inclusion[f"detection:{TARGET}"][0])
        self.assertTrue(composer.required_context_missing)
        self.assertEqual(
            composer.required_context_missing_reason,
            "required_product_projection_excluded",
        )

    def test_history_budget_drops_stale_assistant_report_before_user_context(self) -> None:
        stale_report = "STALE-PEER-LIST " * 4000
        messages = [
            {"role": "user", "content": "Earlier investigation request"},
            {"role": "assistant", "content": stale_report},
            {"role": "user", "content": "Keep this user constraint"},
        ]

        selected = MemoryStore.fit_messages_to_budget(
            messages,
            100,
            prefer_current_evidence=True,
        )

        self.assertNotIn("STALE-PEER-LIST", " ".join(item["content"] for item in selected))
        self.assertIn("Keep this user constraint", [item["content"] for item in selected])
        self.assertIn("STALE-PEER-LIST", messages[1]["content"])

    def test_manifest_rejects_stale_authority_and_total_input_respects_window(self) -> None:
        composer = ContextComposer(settings())
        context_package = package(large_products=True)
        history = MemoryStore.fit_messages_to_budget(
            [{"role": "assistant", "content": "old peer report " * 5000}],
            1000,
            prefer_current_evidence=True,
        )
        fixed_tokens = 5500 + sum(approx_tokens(item["content"]) for item in history)
        text = composer.compose(context_package, base_input_tokens=fixed_tokens)
        coverage = manifest(composer)["provider_coverage"]["graph"]

        self.assertIn("previous assistant claims are conversation only", composer.last_parts["status"])
        self.assertEqual(history, [])
        self.assertLessEqual(
            fixed_tokens + approx_tokens(text) + 12288 + 2048,
            32768,
        )
        self.assertTrue(coverage["model_input_graph_included"])

    def test_impossibly_small_budget_marks_required_graph_missing(self) -> None:
        composer = ContextComposer(
            settings(
                llm_context_window_tokens=2500,
                llm_reserved_output_tokens=1200,
                llm_context_safety_margin_tokens=800,
                graph_max_context_tokens=300,
            )
        )
        text = composer.compose(package(), base_input_tokens=200)
        graph_coverage = manifest(composer)["provider_coverage"]["graph"]

        self.assertTrue(composer.required_context_missing)
        self.assertEqual(composer.last_parts["graph"], "")
        self.assertFalse(graph_coverage["model_input_graph_included"])
        self.assertFalse(graph_coverage["complete_for_user_request"])
        self.assertIn("[SOORIN_PROVIDER_MANIFEST]", text)
        self.assertEqual(composer.required_context_missing_reason, "required_graph_context_excluded")

    def test_node_summary_and_direct_relationship_remain_on_existing_representation(self) -> None:
        graph = exhaustive_graph()
        graph.context.update(
            {
                "scope": "node_summary",
                "requested_scope": "node_summary",
                "depth": 0,
                "exhaustive_connections_requested": False,
            }
        )
        composer = ContextComposer(settings())
        composer.compose(
            CopilotContextPackage(entities=EntityResolver().resolve(TARGET), graph=graph)
        )
        self.assertEqual(graph.context["context_node_count"], 1)
        self.assertEqual(graph.context["context_edge_count"], 0)
        self.assertNotIn('"representation":"compact"', composer.last_parts["graph"])

        direct = exhaustive_graph()
        direct.context.update(
            {
                "scope": "one_hop",
                "requested_scope": "one_hop",
                "exhaustive_connections_requested": False,
                "relationship_mode": "direct",
                "source": TARGET,
                "target": "10.0.0.1",
                "source_present": True,
                "target_present": True,
                "forward_edge": True,
                "reverse_edge": False,
                "relationship_status": "directed",
            }
        )
        composer.compose(
            CopilotContextPackage(entities=EntityResolver().resolve(TARGET), graph=direct)
        )
        self.assertIn('"direct_relationship"', composer.last_parts["graph"])
        self.assertEqual(direct.context["model_context_token_cap"], 700)
        self.assertLessEqual(direct.context["model_context_token_estimate"], 700)

    def test_calibrated_estimate_and_dynamic_output_reservation_are_bounded(self) -> None:
        estimator = TokenEstimator(deployment="gpt55", model="fixture", multiplier=1.35)
        estimate = estimator.estimate_text("x" * 4000)
        self.assertEqual(estimate.raw_tokens, 1000)
        self.assertEqual(estimate.calibrated_tokens, 1350)
        configured = settings()
        reservation_kwargs = {
            "brief_output_tokens": configured.synthesizer_brief_output_tokens,
            "standard_output_tokens": configured.synthesizer_standard_output_tokens,
            "deep_output_tokens": configured.synthesizer_deep_output_tokens,
        }
        self.assertEqual(estimator.output_reservation("brief", 12000, **reservation_kwargs), 1536)
        self.assertEqual(estimator.output_reservation("standard", 12000, **reservation_kwargs), 4096)
        self.assertEqual(estimator.output_reservation("deep", 5000, **reservation_kwargs), 5000)

        custom = settings(
            synthesizer_brief_output_tokens=700,
            synthesizer_standard_output_tokens=1700,
            synthesizer_deep_output_tokens=2700,
        )
        custom_reservation_kwargs = {
            "brief_output_tokens": custom.synthesizer_brief_output_tokens,
            "standard_output_tokens": custom.synthesizer_standard_output_tokens,
            "deep_output_tokens": custom.synthesizer_deep_output_tokens,
        }
        self.assertEqual(estimator.output_reservation("brief", 12000, **custom_reservation_kwargs), 700)
        self.assertEqual(estimator.output_reservation("standard", 12000, **custom_reservation_kwargs), 1700)
        self.assertEqual(estimator.output_reservation("deep", 12000, **custom_reservation_kwargs), 2700)

        composer = ContextComposer(settings(llm_context_window_tokens=8000))
        composer.compose(
            CopilotContextPackage(entities=EntityResolver().resolve(TARGET)),
            base_input_tokens=1000,
            reserved_output_tokens=1536,
        )
        budget = composer.last_budget
        self.assertLessEqual(
            budget["base_input_tokens"]
            + budget["calibrated_dynamic_capacity"]
            + budget["reserved_output_tokens"]
            + composer.settings.llm_context_safety_margin_tokens,
            composer.settings.llm_context_window_tokens,
        )


if __name__ == "__main__":
    unittest.main()
