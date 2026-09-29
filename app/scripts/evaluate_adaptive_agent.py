#!/usr/bin/env python3
"""Run safe adaptive-agent replay evaluation and emit a bounded JSON report."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys

from src.config.settings import get_settings
from src.core.agent.evaluation import (
    AdaptiveEvaluationError,
    load_scenarios,
    load_thresholds,
    run_evaluation,
)
from src.core.agent.investigator import Investigator
from src.core.llm.client import LLMClient
from src.core.observability.llm_usage import UsageCollector


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCENARIOS = ROOT / "app/evals/adaptive_agent/scenarios.jsonl"
DEFAULT_THRESHOLDS = ROOT / "app/evals/adaptive_agent/thresholds.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate bounded adaptive orchestration without live capability execution or memory writes."
    )
    parser.add_argument("--mode", choices=("replay", "model-replay"), default="replay")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--thresholds", type=Path, default=DEFAULT_THRESHOLDS)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--include-results",
        action="store_true",
        help="Include bounded per-scenario structural results in the JSON report.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        scenarios = load_scenarios(args.scenarios)
        thresholds = load_thresholds(args.thresholds)
        settings = get_settings()
        investigator = None
        if args.mode == "model-replay":
            deployment = settings.deployment_for_purpose("investigator")
            if (
                not settings.llm_enabled
                or not deployment.endpoint
                or not deployment.model
                or deployment.model.startswith("CHANGE_ME")
            ):
                report = run_evaluation(
                    scenarios,
                    thresholds,
                    mode="model-replay",
                    investigator=None,
                    settings=settings,
                )
                _write_report(args.output, report.to_json(include_results=args.include_results))
                print("model-replay unavailable: configure the Investigator deployment explicitly")
                print(f"readiness={report.readiness} status={report.status}")
                return 2
            model_settings = replace(settings, adaptive_agent_enabled=True)
            usage = UsageCollector(request_id="adaptive-model-replay", trace_id="adaptive-model-replay")
            client = LLMClient(model_settings, usage_recorder=usage)
            investigator = Investigator(
                client,
                system_prompt_path=model_settings.investigator_system_prompt_path,
            )
            settings = model_settings
        report = run_evaluation(
            scenarios,
            thresholds,
            mode=args.mode,
            investigator=investigator,
            settings=settings,
        )
    except (AdaptiveEvaluationError, OSError, ValueError) as exc:
        print(f"adaptive evaluation configuration error: {exc}", file=sys.stderr)
        return 2

    _write_report(args.output, report.to_json(include_results=args.include_results))
    print(
        f"mode={report.evaluation_mode} scenarios={report.scenario_count} "
        f"passed={report.passed_count} failed={report.failed_count} "
        f"readiness={report.readiness}"
    )
    print(
        f"success={report.metrics.get('scenario_success_rate', 0):.2f}% "
        f"gap_completion={report.metrics.get('required_gap_completion_rate', 0):.2f}% "
        f"tool_precision={report.metrics.get('tool_selection_precision', 0):.2f}%"
    )
    return 0 if report.readiness != "NOT_READY" else 1


def _write_report(path: Path | None, content: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
