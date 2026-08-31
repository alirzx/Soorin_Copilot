"""Focused offline tests for human traces, rotating logs, and safe snapshots."""

from __future__ import annotations

import io
import json
import logging
import os
import time
import sys
from pathlib import Path
from types import SimpleNamespace

from src.core.copilot.trace import CopilotRequestTrace, render_human_copilot_trace
from src.core.observability.logging import JsonLogFormatter, configure_application_logging
from src.core.observability.snapshots import EvidenceSnapshotWriter


class _Stream(io.StringIO):
    def __init__(self, *, tty: bool, encoding: str = "utf-8") -> None:
        super().__init__()
        self._tty = tty
        self._encoding = encoding

    @property
    def encoding(self) -> str:
        return self._encoding

    def isatty(self) -> bool:
        return self._tty


def _trace_settings(*, detail: str = "detailed", color: str = "never") -> SimpleNamespace:
    return SimpleNamespace(
        log_format="console",
        log_color=color,
        copilot_human_trace_detail=detail,
    )


def _full_trace() -> CopilotRequestTrace:
    trace = CopilotRequestTrace("req-1", "session-1", "Investigate 192.0.2.10")
    sections = {
        "REQUEST": {"streaming_requested": True},
        "ROUTER INPUT": {"entity_status": "resolved"},
        "ROUTING STATE": {"previous_intent": "asset_investigation"},
        "ENTITY": {"value": "192.0.2.10", "source": "message"},
        "ENTITY BINDING": {"requested_entity_binding": "explicit"},
        "INTENT": {"intent": "asset_investigation", "semantic_router_latency_ms": 41},
        "AGENT TASK": {"workflow_mode": "multi_step", "recommended_steps": 4},
        "WORKFLOW SELECTION": {"mode": "multi_step"},
        "PLANNER": {"called": True, "source": "llm", "latency_ms": 52},
        "EXECUTION PLAN": {"plan_id": "plan-1", "step_count": 4},
        "PLAN VALIDATION": {"validated": True, "max_calls": 6},
        "CAPABILITY STEPS": {
            "statuses": "profile:ok",
            "details": [
                {
                    "capability": "asset.get_profile",
                    "status": "ok",
                    "views": "overview, identity_role",
                    "detail": "deep",
                    "purpose": "establish_identity",
                    "raw_tokens": 7414,
                    "projected_tokens": 3192,
                    "included_path_count": 153,
                    "omitted_path_count": 0,
                    "context_included": True,
                    "supplemental_local_view": False,
                }
            ],
        },
        "ASSET PROFILE": {"status": "available"},
        "ASSET DETECTION": {"status": "available"},
        "GRAPH RETRIEVAL": {"status": "available", "requested_scope_complete": True},
        "GRAPH COVERAGE": {"complete_for_user_request": True},
        "KNOWLEDGE RETRIEVAL": {
            "status": "unavailable",
            "purpose": "interpret_evidence",
            "normalized_query_hash": "abc123",
            "safe_error_code": "embedding_revision_not_cached",
        },
        "EVIDENCE COVERAGE": {"graph_completeness": "complete"},
        "REVIEW DECISION": {"outcome": "answer_with_limitations"},
        "SUPPLEMENTAL RETRIEVAL": {"count": 0},
        "CONTEXT": {"profile_tokens": 100, "context_inclusion": "profile:included"},
        "MODEL INPUT": {"deployment": "gpt55"},
        "TOKEN BUDGET": {
            "raw_estimate": 1000,
            "calibrated_estimate": 1350,
            "selected_output_reservation": 4096,
            "llm_context_safety_margin_tokens": 2048,
            "provider_prompt_usage": 1400,
            "estimate_to_actual_ratio": 0.96,
        },
        "SYNTHESIS": {"deployment": "gpt55"},
        "MODEL RESPONSE": {"provider": "arvan", "provider_latency_ms": 75},
        "SNAPSHOT": {"status": "written", "mode": "summary", "files": 5},
        "STATE UPDATE": {"active_ip_after": "192.0.2.10"},
        "RESULT": {"status": "ok", "total_latency_ms": 200},
    }
    for name, values in sections.items():
        trace.put(name, **values)
    return trace


def _log_settings(path: Path, *, max_bytes: int = 4096, backups: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        log_level="INFO",
        log_format="console",
        log_file_enabled=True,
        log_file_path=str(path),
        log_file_level="INFO",
        log_file_max_bytes=max_bytes,
        log_file_backup_count=backups,
    )


def _snapshot_settings(root: Path, **overrides: object) -> SimpleNamespace:
    values = {
        "evidence_snapshot_enabled": True,
        "evidence_snapshot_mode": "summary",
        "evidence_snapshot_root": str(root),
        "evidence_snapshot_ttl_hours": 48,
        "evidence_snapshot_max_requests": 100,
        "evidence_snapshot_max_total_bytes": 268435456,
        "evidence_snapshot_max_bytes": 5242880,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _snapshot_artifacts() -> dict[str, object]:
    return {
        "task": {
            "request": "do not store the full request",
            "intent": "asset_investigation",
            "scope": "node_summary",
            "direction": "both",
            "entities": ["192.0.2.10"],
            "required_capabilities": ["asset.get_profile"],
            "workflow_mode": "direct",
            "recommended_steps": 1,
            "detail_level": "deep",
        },
        "plan": {
            "plan_id": "plan-1",
            "source": "deterministic",
            "goal": "bounded investigation",
            "validated": True,
            "maximum_allowed_calls": 6,
            "steps": [
                {
                    "id": "profile-1",
                    "capability": "asset.get_profile",
                    "arguments": {
                        "entities": ["192.0.2.10"],
                        "views": ["overview"],
                        "api_key": "must-not-appear",
                    },
                }
            ],
        },
        "tool-results": [
            {
                "step_id": "profile-1",
                "source_capability": "asset.get_profile",
                "provider": "product",
                "status": "ok",
                "entities": ["192.0.2.10"],
                "latency_ms": 20,
                "freshness": "current",
                "completeness": "complete",
                "selected_views": ["overview"],
                "detail": "deep",
                "purpose": "establish_identity",
                "payload_inventory": {"raw_bytes": 5000, "approx_tokens": 1200},
                "included_paths": ["identity.role"],
                "omitted_section_count": 3,
                "view_token_estimate": 300,
                "context_included": True,
                "raw_payload": {"hostname": "must-not-appear", "password": "must-not-appear"},
            }
        ],
        "review": {
            "outcome": "sufficient",
            "reasons": ["required evidence is present"],
            "supplemental_allowed": False,
        },
        "manifest": {
            "request_id": "req-1",
            "trace_id": "trace-1",
            "plan_id": "plan-1",
            "providers": ["asset.get_profile"],
            "targets": ["192.0.2.10"],
            "context_inclusion": {"asset_profile:192.0.2.10": [True, None]},
            "context_tokens": 300,
        },
        "model-context.redacted": {"content": "must-not-appear"},
    }


def test_summary_and_detailed_trace_modes(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    trace = _full_trace()
    summary = render_human_copilot_trace(
        trace, settings=_trace_settings(detail="summary"), stream=_Stream(tty=False), emit=False
    )
    detailed = render_human_copilot_trace(
        trace, settings=_trace_settings(detail="detailed"), stream=_Stream(tty=False), emit=False
    )
    assert "ROUTING" in summary
    assert "ROUTER INPUT" not in summary
    for section in (
        "ROUTER INPUT", "ROUTING STATE", "ENTITY BINDING", "ROUTER", "AGENT TASK",
        "PLANNER", "EXECUTION PLAN", "PLAN VALIDATION", "CAPABILITY STEPS",
        "ASSET PROFILE", "ASSET DETECTION", "GRAPH RETRIEVAL", "GRAPH COVERAGE",
        "KNOWLEDGE RETRIEVAL", "EVIDENCE COVERAGE", "EVIDENCE REVIEW",
        "SUPPLEMENTAL RETRIEVAL", "MODEL INPUT", "SYNTHESIS", "STATE UPDATE", "RESULT",
    ):
        assert section in detailed
    for field in (
        "recommended steps", "views", "raw tokens", "projected tokens",
        "normalized query hash", "configured safety margin", "provider prompt usage",
        "estimate/actual ratio", "SNAPSHOT", "safe error code",
    ):
        assert field in detailed

    colored = render_human_copilot_trace(
        trace, settings=_trace_settings(color="auto"), stream=_Stream(tty=True), emit=False
    )
    plain = render_human_copilot_trace(
        trace, settings=_trace_settings(color="auto"), stream=_Stream(tty=False), emit=False
    )
    assert "\033[" in colored
    assert "\033[" not in plain
    monkeypatch.setenv("NO_COLOR", "1")
    no_color = render_human_copilot_trace(
        trace, settings=_trace_settings(color="always"), stream=_Stream(tty=True), emit=False
    )
    assert "\033[" not in no_color


def test_rotating_file_logging_is_plain_complete_and_idempotent(tmp_path):
    path = tmp_path / "nested" / "soorin.log"
    settings = _log_settings(path, max_bytes=1024, backups=2)
    test_logger = logging.getLogger(f"test.observability.{time.time_ns()}")
    configure_application_logging(settings, target_logger=test_logger)
    configure_application_logging(settings, target_logger=test_logger)
    assert len([item for item in test_logger.handlers if getattr(item, "_soorin_managed", False)]) == 2
    test_logger.info("event=step_completed request_id=req-1 status=ok")
    render_human_copilot_trace(
        _full_trace(),
        settings=_trace_settings(color="always"),
        stream=_Stream(tty=True),
        output_logger=test_logger,
    )
    for handler in test_logger.handlers:
        handler.flush()
    initial_content = "".join(
        item.read_text(encoding="utf-8") for item in path.parent.glob("soorin.log*")
    )
    assert "event=step_completed" in initial_content
    assert "REQUEST req-1" in initial_content
    assert "\033[" not in initial_content
    for index in range(80):
        test_logger.info("event=rotation_probe index=%s payload=%s", index, "x" * 80)
    for handler in test_logger.handlers:
        handler.flush()
    files = sorted(path.parent.glob("soorin.log*"))
    assert path.exists()
    assert len(files) <= 3
    content = "".join(item.read_text(encoding="utf-8") for item in files)
    assert "\033[" not in content
    for handler in list(test_logger.handlers):
        test_logger.removeHandler(handler)
        handler.close()


def test_json_logging_preserves_bounded_redacted_exception_diagnostics():
    formatter = JsonLogFormatter()
    try:
        raise RuntimeError(
            "password=do-not-emit access_token=token-secret api_key=key-secret "
            "x-hwid=hwid-secret"
        )
    except RuntimeError:
        record = logging.LogRecord(
            "test.json",
            logging.ERROR,
            __file__,
            1,
            "event=product_call_failed request_id=req-1 status=error",
            (),
            sys.exc_info(),
        )
    payload = json.loads(formatter.format(record))
    assert payload["event"] == "product_call_failed"
    assert payload["request_id"] == "req-1"
    assert payload["error_type"] == "RuntimeError"
    assert "RuntimeError" in payload["exception"]
    assert len(payload["exception"]) <= 2000
    for secret in ("do-not-emit", "token-secret", "key-secret", "hwid-secret"):
        assert secret not in json.dumps(payload)


def test_metadata_and_summary_snapshots_are_safe_and_private(tmp_path):
    metadata_writer = EvidenceSnapshotWriter(
        _snapshot_settings(tmp_path / "metadata", evidence_snapshot_mode="metadata")
    )
    metadata_path = metadata_writer.write(
        "metadata-1",
        {"inventory": {"source_hash": "abc123", "hostname": "dc-1", "items": [1, 2]}},
    )
    assert metadata_path is not None
    metadata = json.loads((metadata_path / "inventory.json").read_text(encoding="utf-8"))
    assert metadata["source_hash"] == "abc123"
    assert metadata["hostname"] == {"type": "string", "chars": 4}
    assert metadata["items"] == {"type": "array", "length": 2}

    summary_writer = EvidenceSnapshotWriter(_snapshot_settings(tmp_path / "summary"))
    summary_path = summary_writer.write("summary-1", _snapshot_artifacts())
    assert summary_path is not None
    combined = "".join(path.read_text(encoding="utf-8") for path in summary_path.glob("*.json"))
    tool_results = json.loads((summary_path / "tool-results.json").read_text(encoding="utf-8"))
    manifest = json.loads((summary_path / "manifest.json").read_text(encoding="utf-8"))
    assert tool_results[0]["purpose"] == "establish_identity"
    assert tool_results[0]["raw_size_metadata"]["raw_bytes"] == 5000
    assert manifest["snapshot_mode"] == "summary"
    assert "raw_payload" not in combined
    assert "must-not-appear" not in combined
    assert not (summary_path / "model-context.redacted.json").exists()
    assert summary_writer.root.stat().st_mode & 0o777 == 0o700
    assert summary_path.parent.stat().st_mode & 0o777 == 0o700
    assert summary_path.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in summary_path.glob("*.json"))


def test_snapshot_retention_by_ttl_count_and_total_size(tmp_path):
    ttl_writer = EvidenceSnapshotWriter(
        _snapshot_settings(tmp_path / "ttl", evidence_snapshot_ttl_hours=1)
    )
    old_path = ttl_writer.write("old", _snapshot_artifacts())
    assert old_path is not None
    old_time = time.time() - 7200
    os.utime(old_path, (old_time, old_time))
    current_path = ttl_writer.write("current", _snapshot_artifacts())
    assert current_path is not None and current_path.exists()
    assert not old_path.exists()

    count_writer = EvidenceSnapshotWriter(
        _snapshot_settings(tmp_path / "count", evidence_snapshot_max_requests=2)
    )
    first = count_writer.write("first", _snapshot_artifacts())
    assert first is not None
    os.utime(first, (old_time, old_time))
    count_writer.write("second", _snapshot_artifacts())
    count_writer.write("third", _snapshot_artifacts())
    assert len(count_writer._request_directories()) == 2
    assert not first.exists()

    size_writer = EvidenceSnapshotWriter(
        _snapshot_settings(
            tmp_path / "size",
            evidence_snapshot_mode="redacted",
            evidence_snapshot_max_total_bytes=1200,
            evidence_snapshot_max_bytes=5000,
        )
    )
    old_size_path = size_writer.write("old-size", {"safe_data": "x" * 900})
    assert old_size_path is not None
    os.utime(old_size_path, (old_time, old_time))
    new_size_path = size_writer.write("new-size", {"safe_data": "y" * 900})
    assert new_size_path is not None and new_size_path.exists()
    assert not old_size_path.exists()


def test_snapshot_cleanup_and_write_failures_do_not_escape(tmp_path, monkeypatch):
    cleanup_writer = EvidenceSnapshotWriter(_snapshot_settings(tmp_path / "cleanup"))

    def fail_cleanup():
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(cleanup_writer, "_request_directories", fail_cleanup)
    assert cleanup_writer.write("cleanup-failure", _snapshot_artifacts()) is not None

    write_writer = EvidenceSnapshotWriter(_snapshot_settings(tmp_path / "write"))

    def fail_write(path: Path, content: bytes):
        raise OSError("simulated write failure")

    monkeypatch.setattr(write_writer, "_atomic_write", fail_write)
    assert write_writer.write("write-failure", _snapshot_artifacts()) is None
    assert write_writer.last_result["status"] == "failed"
