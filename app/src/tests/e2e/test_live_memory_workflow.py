"""One guarded, single-pass Product/API T01-T15 validation."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from src.config.settings import get_settings
from src.tests.e2e.live_e2e_helpers import (
    CANONICAL_LOG,
    REPO_ROOT,
    SCENARIOS,
    ManagedApi,
    api_base_url,
    assert_allowed_room,
    assert_canonical_log_guard,
    login_product,
    require_live_opt_in,
    run_one_scenario,
    write_json,
)


pytestmark = pytest.mark.live_e2e


def _report(run_id: str, settings, api_url: str, rooms: dict[str, object], results: list[dict], restart: dict, setup_error: str = "") -> str:
    branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=REPO_ROOT, text=True).strip()
    commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, text=True).strip()
    passed = all(item.get("classification") == "PASS" for item in results) and restart.get("status") == "restarted"
    lines = [
        "# Soorin Copilot Live E2E Validation",
        "",
        f"Run ID: `{run_id}`",
        "",
        "## Environment",
        "",
        f"- Branch/commit: `{branch}` / `{commit}`",
        f"- API base: `{api_url}` (local-only)",
        f"- Product base configured: `{bool(settings.product_api_base_url)}`",
        f"- Provider/model: `{settings.llm_provider}` / `{settings.synthesizer_model}`",
        f"- Memory backend/vector: `{settings.long_term_memory_backend}` / `{bool(settings.memory_vector_index_enabled)}`",
        f"- Memory collection: `{settings.memory_qdrant_collection}`",
        f"- Embedding model: `{settings.rag_embedding_model}`",
        f"- Canonical log: `{CANONICAL_LOG}` (not copied or modified by harness)",
        f"- Setup error: `{setup_error or 'none'}`",
        "",
        "## Scenario matrix T01-T15",
        "",
        "| ID | Room | HTTP | Response | Done | Transcript | Classification | Request ID |",
        "|---|---|---:|---|---|---|---|---|",
    ]
    for item in results:
        scenario = item.get("scenario", {})
        lines.append(
            f"| {scenario.get('scenario_id', '?')} | {item.get('room_id', '?')} | {item.get('http_status', '')} | {item.get('response_nonempty')} | {item.get('done')} | {item.get('product_transcript_verified')} | {item.get('classification')} | {item.get('request_id')} |"
        )
    lines += [
        "",
        "## T16-T20 matrix",
        "",
        "See deterministic pytest results for T16-T20; these are fault-injection tests and are not part of the live T01-T15 sequence.",
        "",
        "## Memory architecture assessment",
        "",
        "T01-T15 artifacts record bounded answer previews, event counts, request-scoped log offsets, and memory/baseline event names. Review durable recall, entity isolation, baseline/current comparison, and analyst-note provenance from the per-scenario review signals.",
        "",
        "## Agent workflow assessment",
        "",
        "The live sequence uses the Product room transcript contract and `/chat/stream`; route/tool behavior must be assessed from the bounded log events and response previews without treating prose alone as proof of tool execution.",
        "",
        "## Token/performance",
        "",
        f"Observed per-turn latency is recorded in each scenario artifact; raw provider token payloads were not stored (`tokens=not_observed`).",
        "",
        "## Bugs",
        "",
        "No automated bug claims are made from prose matching. Transport, transcript, restart, and request-scoped log evidence are recorded per scenario; any defect must cite its scenario, request_id, log event(s), root cause, severity, and release-blocker status.",
        "",
        "## Previous bug verification B1/M1/M2/M3/P1/P2/U1",
        "",
        "The deterministic T16-T20 suite covers the failure-oriented regressions. Live artifacts separately preserve evidence for working fact, pair isolation, broad recall, durable cross-room recall, and current verification.",
        "",
        f"## Conclusion: {'READY TO MERGE DEV → MAIN' if passed else 'NOT READY TO MERGE DEV → MAIN'}",
        "",
        f"T01-T15 transport/transcript/restart gate: `{ 'PASS' if passed else 'FAIL' }`.",
    ]
    return "\n".join(lines) + "\n"


def test_live_t01_t15_once() -> None:
    require_live_opt_in()
    settings = get_settings()
    api_url = api_base_url()
    requested_run_id = os.getenv("SOORIN_E2E_RUN_ID", "").strip()
    run_id = requested_run_id or time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    artifact_dir = REPO_ROOT / "data/runtime/e2e" / run_id
    resume = bool(requested_run_id and artifact_dir.exists())
    if resume:
        raw_rooms = json.loads((artifact_dir / "rooms.json").read_text(encoding="utf-8"))
        rooms: dict[str, object] = dict(raw_rooms)
        results = [
            json.loads((artifact_dir / f"{scenario.scenario_id}.json").read_text(encoding="utf-8"))
            for scenario in SCENARIOS[:11]
        ]
        if len(results) != 11 or any(item.get("classification") != "PASS" for item in results):
            raise RuntimeError("resume requires exactly eleven completed passing primary-room scenarios")
    else:
        artifact_dir.mkdir(parents=True, exist_ok=False)
        artifact_dir.chmod(0o700)
        rooms = {}
        results: list[dict] = []
    api = ManagedApi(api_url)
    restart: dict = {"status": "not_reached"}
    setup_error = ""
    try:
        assert_canonical_log_guard()
        api.ensure()
        llm_health = api.llm_health(settings.copilot_api_key)
        if llm_health.get("status_code") != 200:
            raise RuntimeError("Copilot LLM health preflight failed")
        client, user_id = login_product(settings)
        if resume:
            room_a_id = str(dict(rooms["A"]).get("room_id") or "")
            room_a = client.get_room(room_a_id)
            assert_allowed_room(room_a)
        else:
            room_a = client.create_room(f"soorin-e2e-{run_id}-primary")
            assert_allowed_room(room_a)
            rooms["A"] = {"room_id": room_a.room_id, "title": room_a.title}
            for scenario in SCENARIOS[:11]:
                result = run_one_scenario(scenario=scenario, room=room_a, client=client, user_id=user_id, api_url=api_url, run_id=run_id, settings=settings)
                results.append(result)
                write_json(artifact_dir / f"{scenario.scenario_id}.json", result)
        restart = api.restart_verified_external()
        write_json(artifact_dir / "restart.json", restart)
        scenario = SCENARIOS[11]
        result = run_one_scenario(scenario=scenario, room=room_a, client=client, user_id=user_id, api_url=api_url, run_id=run_id, settings=settings)
        result["restart"] = restart
        results.append(result)
        write_json(artifact_dir / f"{scenario.scenario_id}.json", result)
        room_b = client.create_room(f"soorin-e2e-{run_id}-cross-room")
        assert_allowed_room(room_b)
        rooms["B"] = {"room_id": room_b.room_id, "title": room_b.title}
        for scenario in SCENARIOS[12:]:
            result = run_one_scenario(scenario=scenario, room=room_b, client=client, user_id=user_id, api_url=api_url, run_id=run_id, settings=settings)
            results.append(result)
            write_json(artifact_dir / f"{scenario.scenario_id}.json", result)
    except Exception as exc:
        setup_error = type(exc).__name__
        write_json(artifact_dir / "setup_error.json", {"error_type": setup_error})
        raise
    finally:
        write_json(artifact_dir / "rooms.json", rooms)
        write_json(artifact_dir / "run_summary.json", {"run_id": run_id, "api_base_url": api_url, "rooms": rooms, "restart": restart, "scenario_count": len(results), "tokens": "not_observed"})
        (artifact_dir / "final_report.md").write_text(_report(run_id, settings, api_url, rooms, results, restart, setup_error), encoding="utf-8")
        api.close()
    assert len(results) == 15
    assert all(item["classification"] == "PASS" for item in results)
    assert restart.get("status") == "restarted"
