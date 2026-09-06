"""Offline release contracts that do not require pytest or external services."""

from __future__ import annotations

import io
import logging
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from src.config import settings as settings_module
from src.core.graph.refresh import GraphRefreshService
from src.core.observability.logging import configure_application_logging
from src.core.observability.snapshots import EvidenceSnapshotWriter
from src.tests import test_phase3_langgraph_workflow as phase3_workflow_tests


ROOT = Path(__file__).resolve().parents[3]


def _env_schema(path: Path) -> list[tuple[str, str]]:
    """Return only structure and names, never private values."""
    schema: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            schema.append(("blank", ""))
        elif stripped.startswith("#"):
            schema.append(("comment", stripped))
        elif "=" in line:
            schema.append(("key", line.split("=", 1)[0].strip()))
    return schema


def _snapshot_settings(root: Path, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "evidence_snapshot_enabled": True,
        "evidence_snapshot_mode": "summary",
        "evidence_snapshot_root": str(root),
        "evidence_snapshot_ttl_hours": 1,
        "evidence_snapshot_max_requests": 2,
        "evidence_snapshot_max_total_bytes": 4096,
        "evidence_snapshot_max_bytes": 2048,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class EnvironmentAndRuntimeContractsTests(unittest.TestCase):
    def test_private_and_example_env_have_identical_structure(self) -> None:
        private_path = ROOT / ".env"
        example_path = ROOT / ".env.example"
        self.assertTrue(private_path.exists())
        private_schema = _env_schema(private_path)
        example_schema = _env_schema(example_path)
        self.assertEqual(private_schema, example_schema)
        keys = [value for kind, value in example_schema if kind == "key"]
        self.assertEqual(len(keys), len(set(keys)))

    def test_legacy_split_env_files_are_absent(self) -> None:
        self.assertFalse((ROOT / "app" / ".env").exists())
        self.assertFalse((ROOT / "compose.env").exists())

    def test_compose_uses_fail_fast_api_binds_and_keeps_ui_off_local_graph_data(self) -> None:
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        api, ui = compose.split("  ui:", 1)
        self.assertIn("source: ./data", api)
        self.assertIn("target: /workspace/data", api)
        self.assertIn("SOORIN_RAG_QDRANT_PATH: /workspace/data/qdrant-local", api)
        self.assertIn("create_host_path: false", api)
        self.assertIn("read_only: false", api)
        self.assertNotIn("source: ./data", ui)
        self.assertNotIn("target: /workspace/data", ui)
        self.assertIn("SOORIN_API_BASE_URL: http://api:6998", ui)
        self.assertIn('SOORIN_NEO4J_PASSWORD: ""', ui)
        self.assertNotIn("SOORIN_RAG_SOURCE_HOST_PATH", compose)
        self.assertNotIn("copilot-qdrant", compose)
        self.assertNotIn("copilot-data", compose)

    def test_makefile_has_preflight_without_obsolete_volume_seeding(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("preflight:", makefile)
        self.assertIn("deploy: preflight build preflight-image", makefile)
        self.assertIn("test-local:", makefile)
        self.assertIn("inspect-size:", makefile)
        self.assertNotIn("seed-qdrant", makefile)
        self.assertNotIn('"$(DATA_DIR)/processed/topology_graph.pkl"', makefile)

    def test_checkpoint_runtime_is_absent_from_source_config_and_dependencies(self) -> None:
        workflow = (ROOT / "app/src/core/agent/workflow.py").read_text(encoding="utf-8")
        settings = (ROOT / "app/src/config/settings.py").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn("SqliteSaver", workflow)
        self.assertNotIn("checkpointer=", workflow)
        self.assertIn("SOORIN_LANGGRAPH_CHECKPOINT_BACKEND", settings + env_example)
        self.assertIn("SOORIN_LANGGRAPH_CHECKPOINT_BACKEND=none", env_example)
        self.assertNotIn("langgraph-checkpoint-sqlite", requirements)
        self.assertFalse((ROOT / "app/src/core/agent/checkpoints.py").exists())

    def test_docker_runtime_is_cpu_only_and_excludes_local_data(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("COPY --from=builder /opt/venv /opt/venv", dockerfile)
        self.assertNotIn("COPY --from=builder /build/wheels", dockerfile)
        self.assertIn("torch.version.cuda is None", dockerfile)
        self.assertIn('startswith(("nvidia-", "cuda-"))', dockerfile)
        self.assertIn("USER soorin", dockerfile)
        self.assertIn("app/.env", dockerignore)
        self.assertIn("data/", dockerignore)
        self.assertIn("*.sqlite3", dockerignore)

    def test_qdrant_collection_is_explicit_and_current(self) -> None:
        settings = settings_module.get_settings()
        self.assertEqual(settings.rag_collection, "soorin_soc_knowledge_bge_base_v1")
        self.assertIn(settings.rag_qdrant_mode, {"local", "server"})

    def test_bounded_workflow_operates_without_checkpointing(self) -> None:
        for test_case in (
            phase3_workflow_tests.test_graph_contains_required_nodes_and_conditional_routes,
            phase3_workflow_tests.test_direct_request_skips_planner_and_updates_memory_once,
            phase3_workflow_tests.test_planner_request_is_validated_before_execution,
            phase3_workflow_tests.test_invalid_plan_uses_one_validated_deterministic_fallback,
            phase3_workflow_tests.test_supplemental_retrieval_is_bounded_to_one_round,
            phase3_workflow_tests.test_one_workflow_run_synthesizes_and_updates_memory_once,
            phase3_workflow_tests.test_clarification_responds_without_persistent_resume_state,
            phase3_workflow_tests.test_graph_compiles_without_a_checkpointer,
            phase3_workflow_tests.test_concurrent_requests_keep_state_and_results_isolated,
        ):
            with self.subTest(test=test_case.__name__):
                test_case()

    def test_workflow_run_creates_no_sqlite_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                runtime = phase3_workflow_tests.FakeNodeRuntime()
                phase3_workflow_tests.run_workflow(
                    phase3_workflow_tests.BoundedCopilotWorkflow(),
                    runtime,
                    "no-persistence",
                )
            finally:
                os.chdir(previous)
            generated = list(Path(directory).rglob("*.sqlite3*"))
            self.assertEqual(generated, [])


class ObservabilityAndRetentionContractsTests(unittest.TestCase):
    def tearDown(self) -> None:
        for logger_name in list(logging.Logger.manager.loggerDict):
            if not logger_name.startswith("test.release."):
                continue
            logger = logging.getLogger(logger_name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()

    def test_console_only_logging_is_redacted_and_idempotent(self) -> None:
        logger = logging.getLogger(f"test.release.{time.time_ns()}")
        settings = SimpleNamespace(
            log_level="INFO",
            log_format="console",
            log_file_enabled=False,
            log_file_path="unused.log",
            log_file_level="INFO",
            log_file_max_bytes=20 * 1024 * 1024,
            log_file_backup_count=10,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            configure_application_logging(settings, target_logger=logger)
            configure_application_logging(settings, target_logger=logger)
            logger.info("event=probe authorization=Bearer bearer-secret api_key=private")
        handlers = [item for item in logger.handlers if getattr(item, "_soorin_managed", False)]
        self.assertEqual(len(handlers), 1)
        rendered = output.getvalue()
        self.assertIn("authorization=[REDACTED]", rendered)
        self.assertIn("api_key=[REDACTED]", rendered)
        self.assertNotIn("bearer-secret", rendered)
        self.assertNotIn("private", rendered)

    def test_rotating_handler_uses_configured_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs" / "copilot.log"
            logger = logging.getLogger(f"test.release.{time.time_ns()}")
            settings = SimpleNamespace(
                log_level="INFO",
                log_format="console",
                log_file_enabled=True,
                log_file_path=str(path),
                log_file_level="WARNING",
                log_file_max_bytes=20 * 1024 * 1024,
                log_file_backup_count=10,
            )
            configure_application_logging(settings, target_logger=logger)
            file_handlers = [
                item
                for item in logger.handlers
                if getattr(item, "_soorin_handler_kind", "") == "runtime_file"
            ]
            self.assertEqual(len(file_handlers), 1)
            self.assertEqual(file_handlers[0].maxBytes, 20 * 1024 * 1024)
            self.assertEqual(file_handlers[0].backupCount, 10)

    def test_invalid_observability_categories_are_rejected(self) -> None:
        settings = settings_module.get_settings()
        with self.assertRaises(ValueError):
            replace(settings, log_format="yaml").validate_observability_configuration()
        with self.assertRaises(ValueError):
            replace(settings, evidence_snapshot_mode="raw").validate_observability_configuration()

    def test_snapshot_initialization_prunes_expired_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            expired = root / "2020-01-01" / "old-request"
            expired.mkdir(parents=True)
            (expired / "manifest.json").write_text("{}", encoding="utf-8")
            old_time = time.time() - 7200
            os.utime(expired, (old_time, old_time))
            EvidenceSnapshotWriter(_snapshot_settings(root))
            self.assertFalse(expired.exists())

    def test_snapshot_symlink_root_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            outside = base / "outside"
            outside.mkdir()
            root = base / "evidence-link"
            root.symlink_to(outside, target_is_directory=True)
            writer = EvidenceSnapshotWriter(_snapshot_settings(root))
            self.assertIsNone(writer.write("request", {"manifest": {"status": "ok"}}))
            self.assertEqual(list(outside.iterdir()), [])

    def test_graph_snapshot_pruning_enforces_count_ttl_and_symlink_safety(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "topology_raw.json"
            old = root / "topology_raw.snapshot.old.json"
            middle = root / "topology_raw.snapshot.middle.json"
            newest = root / "topology_raw.snapshot.newest.json"
            for path in (old, middle, newest):
                path.write_text("{}", encoding="utf-8")
            now = time.time()
            os.utime(old, (now - 7200, now - 7200))
            os.utime(middle, (now - 20, now - 20))
            os.utime(newest, (now - 10, now - 10))
            external = root / "external.json"
            external.write_text("preserve", encoding="utf-8")
            link = root / "topology_raw.snapshot.link.json"
            link.symlink_to(external)

            GraphRefreshService._prune_snapshots(anchor, "*.snapshot.*.json", 1, 1)

            self.assertFalse(old.exists())
            self.assertFalse(middle.exists())
            self.assertTrue(newest.exists())
            self.assertTrue(link.is_symlink())
            self.assertEqual(external.read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
