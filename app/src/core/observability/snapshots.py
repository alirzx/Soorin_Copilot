"""Bounded, atomic, optional evidence snapshots for local development."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)
_SNAPSHOT_LOCK = threading.Lock()
_SENSITIVE = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|password|captcha|secret|reasoning)",
    re.I,
)
_OMITTED_CONTENT = {
    "raw_payload",
    "provider_result",
    "view_payload",
    "content",
    "text",
    "request",
    "full_prompt",
    "assistant_response",
    "model_context",
}
_SUMMARY_ARTIFACTS = ("task", "plan", "tool-results", "review", "manifest")


class EvidenceSnapshotWriter:
    def __init__(self, settings: Any) -> None:
        self.enabled = bool(settings.evidence_snapshot_enabled)
        self.mode = settings.evidence_snapshot_mode
        self.root = Path(settings.evidence_snapshot_root)
        self.ttl_hours = max(1, int(settings.evidence_snapshot_ttl_hours))
        self.max_requests = max(1, int(getattr(settings, "evidence_snapshot_max_requests", 100)))
        self.max_total_bytes = max(
            1024, int(getattr(settings, "evidence_snapshot_max_total_bytes", 268435456))
        )
        self.max_bytes = max(1024, int(settings.evidence_snapshot_max_bytes))
        self.last_result: dict[str, Any] = {
            "status": "disabled" if not self.enabled else "not_written",
            "mode": self.mode,
        }

    def write(self, request_id: str, artifacts: dict[str, Any]) -> Path | None:
        if not self.enabled or self.mode == "none":
            self.last_result = {"status": "disabled", "mode": self.mode}
            return None

        safe_request_id = _safe_name(request_id)
        date_name = time.strftime("%Y-%m-%d")
        request_dir = self.root / date_name / safe_request_id
        relative_path = f"{date_name}/{safe_request_id}"
        try:
            self._prepare_directories(request_dir)
            safe_artifacts = self._prepare_artifacts(artifacts, relative_path)
            total = 0
            file_count = 0
            for name, value in safe_artifacts.items():
                encoded = json.dumps(
                    value,
                    ensure_ascii=False,
                    indent=2,
                    default=_json_default,
                ).encode("utf-8")
                if total + len(encoded) > self.max_bytes:
                    logger.warning(
                        "event=evidence_snapshot_bounded request_id=%s mode=%s max_bytes=%s",
                        safe_request_id,
                        self.mode,
                        self.max_bytes,
                    )
                    break
                self._atomic_write(request_dir / f"{_safe_name(name)}.json", encoded)
                total += len(encoded)
                file_count += 1

            self.last_result = {
                "status": "written",
                "mode": self.mode,
                "file_count": file_count,
                "bytes": total,
                "relative_path": relative_path,
            }
            logger.info(
                "event=evidence_snapshot_written request_id=%s mode=%s file_count=%s bytes=%s path_kind=request_relative",
                safe_request_id,
                self.mode,
                file_count,
                total,
            )
            self._prune(current=request_dir)
            return request_dir
        except (OSError, TypeError, ValueError) as exc:
            self.last_result = {
                "status": "failed",
                "mode": self.mode,
                "safe_error_code": type(exc).__name__,
            }
            logger.warning(
                "event=evidence_snapshot_write_failed request_id=%s mode=%s path_kind=request_relative error_class=%s safe_error_code=snapshot_write_failed",
                safe_request_id,
                self.mode,
                type(exc).__name__,
            )
            return None

    def _prepare_artifacts(
        self,
        artifacts: dict[str, Any],
        relative_path: str,
    ) -> dict[str, Any]:
        if self.mode == "metadata":
            return {name: _metadata_only(value) for name, value in artifacts.items()}
        if self.mode == "summary":
            prepared = {
                name: _summary_artifact(name, artifacts[name])
                for name in _SUMMARY_ARTIFACTS
                if name in artifacts
            }
            manifest = prepared.setdefault("manifest", {})
            if isinstance(manifest, dict):
                manifest.update(
                    {
                        "snapshot_mode": "summary",
                        "file_count": len(prepared),
                        "relative_request_path": relative_path,
                    }
                )
            return prepared
        return {
            name: _redact(value)
            for name, value in artifacts.items()
            if name not in {"model-context.redacted", "model-context", "prompt", "response"}
        }

    def _prepare_directories(self, request_dir: Path) -> None:
        date_dir = request_dir.parent
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        date_dir.mkdir(exist_ok=True, mode=0o700)
        os.chmod(date_dir, 0o700)
        request_dir.mkdir(exist_ok=True, mode=0o700)
        os.chmod(request_dir, 0o700)

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _prune(self, *, current: Path) -> None:
        removed_requests = 0
        freed_bytes = 0
        try:
            with _SNAPSHOT_LOCK:
                request_dirs = self._request_directories()
                cutoff = time.time() - self.ttl_hours * 3600
                for path in list(request_dirs):
                    if path == current:
                        continue
                    if path.stat().st_mtime < cutoff:
                        freed_bytes += _directory_size(path)
                        shutil.rmtree(path)
                        request_dirs.remove(path)
                        removed_requests += 1

                request_dirs.sort(key=lambda item: item.stat().st_mtime)
                while len(request_dirs) > self.max_requests:
                    victim = next((path for path in request_dirs if path != current), None)
                    if victim is None:
                        break
                    freed_bytes += _directory_size(victim)
                    shutil.rmtree(victim)
                    request_dirs.remove(victim)
                    removed_requests += 1

                total_bytes = sum(_directory_size(path) for path in request_dirs)
                while total_bytes > self.max_total_bytes:
                    victim = next((path for path in request_dirs if path != current), None)
                    if victim is None:
                        break
                    size = _directory_size(victim)
                    shutil.rmtree(victim)
                    request_dirs.remove(victim)
                    total_bytes -= size
                    freed_bytes += size
                    removed_requests += 1
                self._remove_empty_date_directories()
        except OSError as exc:
            logger.warning(
                "event=evidence_snapshot_prune_failed path_kind=snapshot_root error_class=%s safe_error_code=snapshot_prune_failed",
                type(exc).__name__,
            )
            return
        if removed_requests:
            logger.info(
                "event=evidence_snapshot_pruned path_kind=snapshot_root removed_requests=%s freed_bytes=%s",
                removed_requests,
                freed_bytes,
            )

    def _request_directories(self) -> list[Path]:
        if not self.root.exists():
            return []
        return [
            request_dir
            for date_dir in self.root.iterdir()
            if date_dir.is_dir()
            for request_dir in date_dir.iterdir()
            if request_dir.is_dir()
        ]

    def _remove_empty_date_directories(self) -> None:
        if not self.root.exists():
            return
        for date_dir in self.root.iterdir():
            if date_dir.is_dir() and not any(date_dir.iterdir()):
                date_dir.rmdir()


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value))[:80] or "snapshot"


def _json_default(value: Any) -> Any:
    return _to_plain(value)


def _to_plain(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


def _redact(value: Any) -> Any:
    value = _to_plain(value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if _SENSITIVE.search(normalized):
                result[str(key)] = "[REDACTED]"
            elif normalized in _OMITTED_CONTENT:
                result[str(key)] = "[OMITTED]"
            else:
                result[str(key)] = _redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _metadata_only(value: Any, *, key: str = "") -> Any:
    redacted = _redact(value)
    if isinstance(redacted, dict):
        return {
            str(child_key): _metadata_only(item, key=str(child_key))
            for child_key, item in redacted.items()
            if str(child_key).lower() not in _OMITTED_CONTENT
        }
    if isinstance(redacted, list):
        return {"type": "array", "length": len(redacted)}
    if isinstance(redacted, str):
        if key.lower().endswith("hash"):
            return redacted
        return {"type": "string", "chars": len(redacted)}
    return redacted


def _summary_artifact(name: str, value: Any) -> Any:
    plain = _to_plain(value)
    if name == "task":
        return _pick(
            plain,
            "intent",
            "scope",
            "direction",
            "entities",
            "required_capabilities",
            "workflow_mode",
            "recommended_steps",
            "detail_level",
            "is_followup",
            "graph_depth",
            "relationship_mode",
        )
    if name == "plan":
        result = _pick(
            plain,
            "plan_id",
            "source",
            "goal",
            "validated",
            "maximum_allowed_calls",
        )
        result["steps"] = [
            {
                **_pick(step, "id", "capability", "depends_on", "requirement"),
                "arguments": _safe_arguments(step.get("arguments", {})),
            }
            for step in plain.get("steps", [])
            if isinstance(step, dict)
        ]
        return _redact(result)
    if name == "tool-results":
        values = plain if isinstance(plain, list) else [plain]
        return [_summarize_tool_result(item) for item in values if isinstance(item, dict)]
    if name == "review":
        result = _pick(
            plain,
            "outcome",
            "reasons",
            "missing_capabilities",
            "limitations",
            "conflicts",
            "supplemental_allowed",
            "next_capability",
        )
        result["next_arguments"] = _safe_arguments(plain.get("next_arguments") or {})
        return _redact(result)
    if name == "manifest":
        return _redact(
            _pick(
                plain,
                "request_id",
                "trace_id",
                "plan_id",
                "providers",
                "targets",
                "context_inclusion",
                "context_tokens",
            )
        )
    return _metadata_only(plain)


def _summarize_tool_result(item: dict[str, Any]) -> dict[str, Any]:
    result = _pick(
        item,
        "step_id",
        "source_capability",
        "provider",
        "status",
        "entities",
        "latency_ms",
        "freshness",
        "completeness",
        "cache_status",
        "selected_views",
        "detail",
        "purpose",
        "total_count",
        "included_count",
        "omitted_count",
        "truncated",
        "context_included",
        "safe_error_code",
        "normalized_query_hash",
        "view_token_estimate",
        "omitted_section_count",
    )
    result["included_path_count"] = len(item.get("included_paths") or [])
    inventory = item.get("payload_inventory") or {}
    result["raw_size_metadata"] = {
        key: value
        for key, value in inventory.items()
        if isinstance(value, (int, float, bool)) and ("size" in key or "byte" in key or "char" in key or "token" in key or "count" in key)
    }
    return _redact(result)


def _safe_arguments(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {}
    allowed = {
        "entities",
        "target_ip",
        "target_ips",
        "scope",
        "direction",
        "depth",
        "relationship_mode",
        "views",
        "detail",
        "purpose",
        "token_budget",
        "normalized_query_hash",
    }
    return _redact({key: value for key, value in arguments.items() if key in allowed})


def _pick(value: Any, *keys: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in keys if key in value and value[key] not in (None, "")}


def _directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total
