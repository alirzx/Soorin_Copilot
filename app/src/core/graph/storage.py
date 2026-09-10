"""Storage helpers for retained raw Product topology snapshots."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from src.config.settings import APP_DIR


PROJECT_ROOT = APP_DIR.parent
logger = logging.getLogger(__name__)


def resolve_path(path: str | Path) -> Path:
    path_obj = Path(path)
    return path_obj if path_obj.is_absolute() else PROJECT_ROOT / path_obj


def atomic_write_json(payload: Any, path: str | Path) -> Path:
    """Atomically retain a raw Product response for audit and debugging only.

    Temporary files are removed on serialization/write/replace failure so a
    failed refresh cannot accumulate orphan artifacts beside retained snapshots.
    """
    output_path = resolve_path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f"{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(output_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    logger.info("event=graph_raw_snapshot_saved path=%s bytes=%s", path, output_path.stat().st_size)
    return output_path
