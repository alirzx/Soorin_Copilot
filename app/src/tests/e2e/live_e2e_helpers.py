"""Safe, single-pass live E2E helpers for the Product-backed Copilot UI contract.

The live harness deliberately keeps credentials and raw responses in memory only.
Its artifacts contain bounded summaries and byte offsets into the canonical log,
never a copied log or provider payload.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from src.config.settings import Settings
from src.web.chat_stream import ChatStreamProtocolError, parse_sse_events
from src.web.product_user_chat import ProductChatRoom, ProductUserChatClient


REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_API_BASE = "http://127.0.0.1:6998"
CANONICAL_LOG = REPO_ROOT / "data/runtime/logs/soorin-copilot.log"
SAFE_LOG_KEYS = {
    "event", "request_id", "session_id", "conversation_id", "thread_key",
    "trace_id", "status", "reason", "reason_code", "error_type", "fallback",
    "finish_reason", "stream_done", "latency_ms", "duration_ms", "count",
    "selected_count", "candidate_count", "active_count", "baseline_count",
    "memory_count", "entity_count", "provider", "model", "deployment",
    "purpose", "mode", "transition", "episode_transition", "operation",
    "target", "source", "capabilities", "providers", "status_code",
    "allow_live", "require_current", "memory_only", "memory_write",
    "entity_mode", "primary_entity_source", "primary_entity_value",
    "intent", "scope", "direction", "depth", "decision_source",
    "router_called", "planner_called", "provider_called", "routing_state_mutated",
    "capability", "target_ip", "views", "cache_enabled", "cache_age_seconds",
    "cache_miss_reason", "selected_providers", "outcome", "completion_tokens",
    "prompt_tokens", "input_tokens", "output_tokens", "total_tokens", "max_tokens",
    "chunk_count", "reasoning_chunk_count", "answer_chunk_count", "call_count",
    "active_entity_count", "episode_count", "working_fact_count", "write_count",
    "memory_write_count", "projection_count", "comparison_count", "reason_count",
    "candidate_count", "semantic_candidate_count", "exact_candidate_count", "reranked_count",
    "estimated_tokens", "retrieval_complete", "requested_scope_complete", "truncated",
    "context_included", "completeness", "evidence_class", "action", "reasoning_present",
}
SECRET_KEYS = {"message", "exception", "access_token", "authorization", "api_key", "password", "token", "prompt"}


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    title: str
    message: str
    room_key: str
    selected_ip: str = ""


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("T01", "primary comprehensive investigation", "Analyze comprehensively asset 192.168.0.62: its identity, behavior, connections, profile, and whether it is anomalous.", "A", "192.168.0.62"),
    Scenario("T02", "primary memory-only recall", "What did you conclude about this asset before? Use only what you already remember from this conversation. Do not use any live tools or fresh evidence.", "A", "192.168.0.62"),
    Scenario("T03", "primary current verification", "Verify whether those conclusions are still true now. Use the previous investigation as the baseline, then check the current live evidence and tell me what changed, what stayed the same, and what is incomparable.", "A", "192.168.0.62"),
    Scenario("T04", "switch to second asset", "Now analyze comprehensively 192.168.30.115 and tell me about it.", "A", "192.168.30.115"),
    Scenario("T05", "return to first asset memory", "What did we previously conclude about 192.168.0.62 before switching to the other asset? Use only what you already remember from this conversation. Do not use any live tools or fresh evidence.", "A", "192.168.0.62"),
    Scenario("T06", "topic detachment", "Forget the asset investigation for this question. Explain briefly what SIEM means in cybersecurity in general and summary?", "A"),
    Scenario("T07", "analyst memory write", "Remember for this investigation that the analyst label for 192.168.0.62 is \"vxidalira legacy integration server\". Do not treat this as independently verified Product evidence.", "A", "192.168.0.62"),
    Scenario("T08", "analyst memory recall", "What analyst-provided fact did I ask you to remember about 192.168.0.62? Use memory only and do not use any live tools or fresh evidence.", "A", "192.168.0.62"),
    Scenario("T09", "cross-asset comparison", "Compare 192.168.0.62 with 192.168.30.115. Focus on identity, risk, connectivity, and anomaly differences. And tell me what was that analyst label, and for what asset?", "A", "192.168.0.62"),
    Scenario("T10", "cross-asset concern", "Which of these two is more concerning and why? Use the comparison we just established.", "A"),
    Scenario("T11", "conversation summary", "Summarize the main conclusions and analyst-provided notes we have established in this conversation so far. Use only all of our discussion. Do not use live tools or fresh evidence.", "A"),
    Scenario("T12", "post-restart memory", "After the restart, what had we concluded about 192.168.0.62 before, and what analyst-provided label was associated with it? Use only stored memory. Do not use live tools or fresh evidence. Tell me all you know from our conversations.", "A", "192.168.0.62"),
    Scenario("T13", "cross-room durable recall", "What do you already know from durable previous investigations about 192.168.0.62? Use stored memory only. Do not fetch any current or live evidence.", "B", "192.168.0.62"),
    Scenario("T14", "cross-room durable finding", "What durable knowledge do you have about the internal Linux server that had a high risk score but no external connections? Use stored durable memory only and do not refresh anything.", "B"),
    Scenario("T15", "cross-room current verification", "Now verify that remembered finding against the current state of that asset. Tell me what is still supported, what changed, and what cannot be compared reliably.", "B"),
)


def require_live_opt_in() -> None:
    if os.getenv("SOORIN_E2E_LIVE") != "1" or os.getenv("SOORIN_E2E_CONFIRM_REAL_WRITES") != "1":
        import pytest
        pytest.skip("live E2E requires SOORIN_E2E_LIVE=1 and SOORIN_E2E_CONFIRM_REAL_WRITES=1")


def api_base_url() -> str:
    value = (os.getenv("SOORIN_E2E_API_BASE_URL") or DEFAULT_API_BASE).strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.path not in {"", "/"}:
        raise RuntimeError("SOORIN_E2E_API_BASE_URL must be a local HTTP URL.")
    return value


def _safe_log_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"inode": int(stat.st_ino), "size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def assert_canonical_log_guard() -> dict[str, Any]:
    path = Path(os.path.abspath(str(CANONICAL_LOG)))
    if path != CANONICAL_LOG or not path.exists():
        raise RuntimeError("canonical runtime log is missing or not the expected path")
    return _safe_log_identity(path)


def _safe_value(key: str, value: Any) -> Any:
    if key.lower() in SECRET_KEYS:
        return "redacted"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = str(value).strip()
    return text[:160]


def _parse_log_line(line: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    json_start = line.find("{")
    if json_start >= 0:
        try:
            payload = json.loads(line[json_start:])
            if isinstance(payload, dict):
                for key, value in payload.items():
                    if key.lower() not in SECRET_KEYS and key in SAFE_LOG_KEYS:
                        result[key] = _safe_value(key, value)
                return result
        except ValueError:
            pass
    event = re.search(r"\bevent=([^\s]+)", line)
    if event:
        result["event"] = event.group(1)[:160]
    for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)", line):
        key, value = match.groups()
        if key in SAFE_LOG_KEYS and key.lower() not in SECRET_KEYS:
            result[key] = _safe_value(key, value)
    return result


def extract_log_events(before: dict[str, Any], request_id: str, session_id: str) -> dict[str, Any]:
    after = _safe_log_identity(CANONICAL_LOG)
    if after["inode"] != before["inode"] or after["size"] < before["size"]:
        raise RuntimeError("canonical runtime log changed inode or shrank during live run")
    events: list[dict[str, Any]] = []
    with CANONICAL_LOG.open("rb") as handle:
        handle.seek(before["size"])
        segment = handle.read(after["size"] - before["size"])
    for raw in segment.splitlines():
        line = raw.decode("utf-8", errors="replace")
        if request_id not in line and session_id not in line:
            continue
        parsed = _parse_log_line(line)
        if parsed:
            events.append(parsed)
    purposes = {"baseline": [], "memory": [], "ltm": [], "fallback": []}
    for event in events:
        text = " ".join(str(value) for value in event.values()).lower()
        if "baseline" in text:
            purposes["baseline"].append(event.get("event", ""))
        if "memory" in text:
            purposes["memory"].append(event.get("event", ""))
        if "ltm" in text or "long_term" in text:
            purposes["ltm"].append(event.get("event", ""))
        if "fallback" in text or event.get("fallback") in {"true", "1"}:
            purposes["fallback"].append(event.get("event", ""))
    return {"byte_start": before["size"], "byte_end": after["size"], "events": events, "purpose_events": purposes}


class ManagedApi:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.process: subprocess.Popen[bytes] | None = None
        self.started_by_harness = False

    def health(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/health", timeout=3)
            return response.status_code == 200
        except requests.RequestException:
            return False

    def llm_health(self, api_key: str) -> dict[str, Any]:
        try:
            response = requests.get(
                f"{self.base_url}/llm/health",
                headers={"Soorin_copilot_api_key": api_key},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            return {"status_code": response.status_code, "ready": bool(payload.get("data", {}).get("ready"))} if isinstance(payload, dict) else {"status_code": response.status_code}
        except (requests.RequestException, ValueError):
            return {"status": "unavailable"}

    def ensure(self) -> None:
        if self.health():
            return
        if os.getenv("SOORIN_E2E_MANAGE_API") != "1":
            raise RuntimeError("local Copilot API is unavailable; set SOORIN_E2E_MANAGE_API=1 only when harness may start it")
        if Path.cwd().resolve() != REPO_ROOT:
            raise RuntimeError("live harness must run from the canonical repository")
        self.process = subprocess.Popen(
            [sys.executable, "app/run.py", "--api"],
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONPATH": "app"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.started_by_harness = True
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.health():
                return
            if self.process.poll() is not None:
                raise RuntimeError("harness-owned Copilot API exited during startup")
            time.sleep(0.5)
        raise RuntimeError("harness-owned Copilot API did not become healthy")

    def _verified_external_owner(self) -> tuple[int, str]:
        if os.getenv("SOORIN_E2E_ALLOW_EXTERNAL_RESTART") != "1":
            raise RuntimeError("external API restart requires SOORIN_E2E_ALLOW_EXTERNAL_RESTART=1")
        parsed = urlparse(self.base_url)
        if parsed.port != 6998:
            raise RuntimeError("external restart is limited to the canonical local API port")
        try:
            listing = subprocess.check_output(["ss", "-ltnpH"], text=True, stderr=subprocess.STDOUT)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError("unable to inspect API port ownership") from exc
        match = re.search(r"(?:127\.0\.0\.1|0\.0\.0\.0):6998.*?pid=(\d+)", listing)
        if not match:
            raise RuntimeError("no process owns the canonical local API port")
        pid = int(match.group(1))
        proc_path = Path(f"/proc/{pid}")
        try:
            cwd = proc_path.joinpath("cwd").resolve()
            cmdline = proc_path.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
            uid = proc_path.stat().st_uid
        except OSError as exc:
            raise RuntimeError("unable to inspect the local API process") from exc
        if uid != os.getuid() or cwd != REPO_ROOT or "app/run.py --api" not in cmdline:
            raise RuntimeError("local API process is not the verified canonical Copilot command")
        return pid, cmdline

    def _wait_for_health(self, timeout_seconds: float = 45.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.health():
                return
            time.sleep(0.5)
        raise RuntimeError("local Copilot API did not become healthy")

    def restart_owned(self) -> dict[str, Any]:
        if not self.started_by_harness or self.process is None:
            return {"status": "manual_checkpoint_required", "reason": "API was not started by harness"}
        old_pid = self.process.pid
        self.process.terminate()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None
        self.started_by_harness = False
        self.ensure()
        return {"status": "restarted", "old_pid": old_pid, "new_pid": self.process.pid if self.process else None}

    def restart_verified_external(self) -> dict[str, Any]:
        """Restart only the verified canonical API; leave its replacement running."""
        if self.started_by_harness:
            return self.restart_owned()
        old_pid, _command = self._verified_external_owner()
        before = assert_canonical_log_guard()
        os.kill(old_pid, signal.SIGTERM)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not Path(f"/proc/{old_pid}").exists():
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("verified canonical API did not stop after SIGTERM")
        replacement = subprocess.Popen(
            [sys.executable, "app/run.py", "--api"],
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONPATH": "app"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            self._wait_for_health()
        except Exception:
            if replacement.poll() is None:
                replacement.terminate()
            raise
        after = assert_canonical_log_guard()
        if after["inode"] != before["inode"] or after["size"] < before["size"]:
            raise RuntimeError("canonical runtime log changed inode or shrank during API restart")
        return {
            "status": "restarted",
            "ownership": "verified_external_canonical_process",
            "old_pid": old_pid,
            "new_pid": replacement.pid,
            "log_before": before,
            "log_after": after,
        }

    def close(self) -> None:
        if not self.started_by_harness or self.process is None:
            return
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None
        self.started_by_harness = False


def login_product(settings: Settings) -> tuple[ProductUserChatClient, str]:
    required = (settings.product_api_base_url, settings.product_username, settings.product_password, settings.product_hwid)
    if not all(required):
        raise RuntimeError("Product live E2E configuration is incomplete")
    client = ProductUserChatClient(settings)
    session = client.login(settings.product_username, settings.product_password)
    client.token = session.access_token
    return client, session.user_id


def assert_allowed_room(room: ProductChatRoom) -> None:
    if room.room_id == "70":
        raise RuntimeError("room/session 70 is forbidden for live E2E")


def _compact_answer(answer: str) -> str:
    return " ".join(answer.split())[:240]


def run_one_scenario(*, scenario: Scenario, room: ProductChatRoom, client: ProductUserChatClient, user_id: str, api_url: str, run_id: str, settings: Settings) -> dict[str, Any]:
    request_id = f"e2e-{run_id}-{scenario.scenario_id}"
    started = time.perf_counter()
    log_before = assert_canonical_log_guard()
    answer_parts: list[str] = []
    event_counts: dict[str, int] = {}
    done_data: dict[str, Any] = {}
    http_status: int | None = None
    error = ""
    transcript_verified = False
    try:
        client.append_message(room.room_id, "user", scenario.message)
        headers = {
            "Authorization": f"Bearer {client.token}",
            "Soorin_copilot_api_key": settings.copilot_api_key,
            "X-User-ID": user_id,
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "conversation_id": room.room_id,
            "session_id": room.room_id,
            "request_id": request_id,
            "message": scenario.message,
        }
        if scenario.selected_ip:
            body["ui_context"] = {"selected_ip": scenario.selected_ip}
        with requests.post(f"{api_url}/chat/stream", headers=headers, json=body, stream=True, timeout=settings.api_timeout_seconds) as response:
            http_status = response.status_code
            response.raise_for_status()
            response.encoding = "utf-8"
            for event in parse_sse_events(response.iter_lines(chunk_size=1, decode_unicode=True)):
                event_type = str(event.get("type") or "unknown")
                event_counts[event_type] = event_counts.get(event_type, 0) + 1
                if event_type == "answer_delta":
                    answer_parts.append(str(event.get("text") or ""))
                elif event_type == "done":
                    done_data = dict(event.get("data") or {})
                elif event_type == "error":
                    error = "stream_error"
        answer = "".join(answer_parts).strip()
        if done_data and answer:
            client.append_message(room.room_id, "assistant", answer)
        refreshed = client.get_room(room.room_id)
        transcript_verified = len(refreshed.messages) >= 2 and refreshed.messages[-2].role == "user" and refreshed.messages[-1].role == "assistant" and refreshed.messages[-1].content == answer
    except (requests.RequestException, ChatStreamProtocolError, Exception) as exc:
        error = type(exc).__name__
        answer = "".join(answer_parts).strip()
    log_after = _safe_log_identity(CANONICAL_LOG)
    log_data = extract_log_events(log_before, request_id, room.room_id)
    transport_ok = http_status == 200 and bool(answer) and bool(done_data) and not error
    result = {
        "scenario": asdict(scenario),
        "request_id": request_id,
        "session_id": room.room_id,
        "room_id": room.room_id,
        "http_status": http_status,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "response_nonempty": bool(answer),
        "done": bool(done_data),
        "answer_chars": len(answer),
        "answer_contains_label": "vxidalira legacy integration server" in answer.lower(),
        "answer_preview": _compact_answer(answer),
        "product_transcript_verified": transcript_verified,
        "event_counts": event_counts,
        "done_metadata": {key: value for key, value in done_data.items() if key in {"finish_reason", "usage", "trace_id"}},
        "log": log_data,
        "log_before": log_before,
        "log_after": log_after,
        "tokens": "not_observed",
        "error": error,
        "classification": "PASS" if transport_ok and transcript_verified else "FAIL",
    }
    print(f"{scenario.scenario_id} {result['classification']} http={http_status} answer_chars={len(answer)} latency_ms={result['latency_ms']}", flush=True)
    return result


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
