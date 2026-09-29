"""Deterministic release-gate tests for bounded adaptive evaluation."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.config.settings import get_settings
from src.core.agent.evaluation import (
    AdaptiveEvalResult,
    AdaptiveEvaluationError,
    aggregate_metrics,
    calculate_readiness,
    load_scenarios,
    load_thresholds,
    run_evaluation,
)


ROOT = Path(__file__).resolve().parents[3]
SCENARIOS = ROOT / "app/evals/adaptive_agent/scenarios.jsonl"
THRESHOLDS = ROOT / "app/evals/adaptive_agent/thresholds.json"
RUNNER = ROOT / "app/scripts/evaluate_adaptive_agent.py"


def _result(**changes: object) -> AdaptiveEvalResult:
    base = AdaptiveEvalResult(
        scenario_id="scenario",
        categories=("adaptive",),
        success=True,
        failure_reasons=(),
        selected_mode="adaptive",
        stop_reason="evidence_sufficient",
        final_gap_states=(("gap", "satisfied"),),
        investigator_turns=1,
        llm_calls=3,
        capability_calls=1,
        executed_capabilities=("asset.get_profile",),
        rejected_reasons=(),
        decision_kinds=("CONTINUE",),
        tool_true_positives=1,
        unnecessary_tool_calls=0,
        expected_tool_coverage_hits=1,
        expected_tool_coverage_total=1,
        duplicate_provider_calls=0,
        equivalence_suppressions=0,
        authority_violations_proposed=0,
        authority_violations_accepted=0,
        premature_finish_proposed=0,
        premature_finish_accepted=0,
        clarification_correct=None,
        required_gap_total=1,
        required_gap_satisfied=1,
        required_gap_failed=0,
        context_input_tokens_before=(110,),
        context_input_tokens=(100,),
        provider_input_tokens=(),
        context_compaction_savings=10,
        evidence_reference_counts=(1,),
        delta_counts=(1,),
        capability_schema_counts=(1,),
        context_hard_limit_violations=0,
        investigator_output_tokens=(),
        model_latency_ms=0,
        review_outcome="sufficient",
    )
    return replace(base, **changes)


@pytest.fixture(scope="module")
def deterministic_report():
    return run_evaluation(
        load_scenarios(SCENARIOS),
        load_thresholds(THRESHOLDS),
        mode="replay",
        settings=get_settings(),
    )


def test_scenario_schema_rejects_non_rfc5737_address(tmp_path: Path) -> None:
    payload = json.loads(SCENARIOS.read_text(encoding="utf-8").splitlines()[0])
    payload["task"]["request"] = "Investigate 10.0.0.8"
    source = tmp_path / "invalid.jsonl"
    source.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(AdaptiveEvaluationError, match="non-RFC5737"):
        load_scenarios(source)


def test_aggregate_metrics_preserve_quality_and_safety_distinctions() -> None:
    results = (
        _result(
            scenario_id="pass",
            investigator_turns=2,
            capability_calls=2,
            tool_true_positives=2,
            required_gap_total=2,
            required_gap_satisfied=2,
            authority_violations_proposed=1,
            premature_finish_proposed=1,
        ),
        _result(
            scenario_id="fail",
            success=False,
            failure_reasons=("required_gap_incomplete",),
            stop_reason="no_useful_action",
            tool_true_positives=0,
            unnecessary_tool_calls=1,
            required_gap_satisfied=0,
            required_gap_failed=1,
            review_outcome="missing_required_evidence",
        ),
    )
    metrics = aggregate_metrics(results)
    assert metrics["scenario_success_rate"] == 50.0
    assert metrics["required_gap_completion_rate"] == 66.6667
    assert metrics["tool_selection_precision"] == 66.6667
    assert metrics["unnecessary_tool_call_rate"] == 33.3333
    assert metrics["authority_violation_proposed_rate"] > 0
    assert metrics["authority_violation_accepted_rate"] == 0
    assert metrics["premature_finish_proposed_rate"] > 0
    assert metrics["premature_finish_accepted_rate"] == 0


def test_readiness_hard_quality_and_pass_outcomes() -> None:
    assert calculate_readiness(
        mode="replay",
        status="completed",
        hard_gate_results={"authority": False},
        quality_gate_results={"success": True},
    ) == "NOT_READY"
    assert calculate_readiness(
        mode="replay",
        status="completed",
        hard_gate_results={"authority": True},
        quality_gate_results={"success": False},
    ) == "NOT_READY"
    assert calculate_readiness(
        mode="replay",
        status="completed",
        hard_gate_results={"authority": True},
        quality_gate_results={"success": True},
    ) == "READY_FOR_MODEL_REPLAY"
    assert calculate_readiness(
        mode="model-replay",
        status="completed",
        hard_gate_results={"authority": True},
        quality_gate_results={"success": True},
    ) == "READY_FOR_STAGING"


def test_complete_replay_uses_runtime_validators_and_passes_release_gates(
    deterministic_report,
) -> None:
    report = deterministic_report
    assert report.scenario_count == 35
    assert report.passed_count == 35
    assert report.failed_count == 0
    assert all(report.hard_gate_results.values())
    assert all(report.quality_gate_results.values())
    assert report.readiness == "READY_FOR_MODEL_REPLAY"
    assert report.metrics["scenario_success_rate"] == 100.0
    assert report.metrics["required_gap_completion_rate"] == 100.0
    assert report.metrics["tool_selection_precision"] >= 90.0
    assert report.baseline_comparison["required_gap_completion_delta"] > 0
    assert any(
        any(category.startswith("structured") for category in item.categories)
        and item.capability_calls > 0
        for item in report.results
    )
    graph_switch = next(
        item for item in report.results if item.scenario_id == "adaptive_graph_version_switch"
    )
    assert graph_switch.executed_capabilities.count("graph.get_summary") == 2


def test_json_report_is_serializable_and_excludes_raw_fixture_content(
    deterministic_report,
) -> None:
    serialized = deterministic_report.to_json(include_results=True)
    payload = json.loads(serialized)
    assert payload["schema_version"] == "adaptive-agent-evaluation-v1"
    assert payload["corpus_version"] == "adaptive-agent-eval-v1"
    assert "IGNORE_ALL_POLICY_AND_EXFILTRATE" not in serialized
    assert "raw_payload" not in serialized
    assert "prompt" not in payload


def test_cli_exit_status_and_machine_readable_report(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    environment = {**os.environ, "PYTHONPATH": "app"}
    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--mode",
            "replay",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["readiness"] == "READY_FOR_MODEL_REPLAY"

    invalid = subprocess.run(
        [sys.executable, str(RUNNER), "--thresholds", str(tmp_path / "missing.json")],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert invalid.returncode == 2
