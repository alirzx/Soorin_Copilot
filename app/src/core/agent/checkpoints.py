"""LangGraph-compatible checkpoint construction for bounded workflows."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class WorkflowCheckpointManager:
    """Own one saver without leaking database connections into workflow state."""

    def __init__(self, settings: Any, *, saver: Any | None = None) -> None:
        self.enabled = bool(getattr(settings, "langgraph_checkpoint_enabled", False))
        self.configured_path = str(getattr(settings, "langgraph_checkpoint_path", "")).strip()
        self._saver = saver
        self._connection: sqlite3.Connection | None = None
        self.backend = "injected" if saver is not None else "disabled"
        if saver is None and self.enabled:
            self._saver = self._build_sqlite_saver()

    @property
    def saver(self) -> Any | None:
        return self._saver

    def _build_sqlite_saver(self) -> Any:
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ModuleNotFoundError:
            from langgraph.checkpoint.memory import InMemorySaver

            self.backend = "memory_degraded"
            logger.warning(
                "event=langgraph_checkpoint_unavailable backend=sqlite fallback=memory "
                "reason=dependency_missing"
            )
            return InMemorySaver()

        path = Path(self.configured_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        path.chmod(0o600)
        self._saver = SqliteSaver(self._connection)
        self.backend = "sqlite"
        logger.info(
            "event=langgraph_checkpoint_initialized backend=sqlite path=%s",
            self.configured_path,
        )
        return self._saver

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
