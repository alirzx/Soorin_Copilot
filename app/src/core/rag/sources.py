"""Explicit source discovery and loading for an external SOC corpus."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


ALLOWED_SUFFIXES = {".txt", ".md", ".markdown", ".json", ".csv"}


@dataclass(frozen=True)
class SourceDocument:
    document_id: str
    source_path: str
    relative_path: str
    title: str
    category: str
    text: str
    content_hash: str
    source_version: str


def validate_source_root(source_root: str | Path) -> Path:
    raw = str(source_root).strip()
    if not raw:
        raise ValueError("RAG source root is not configured.")
    root = Path(raw).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError("Configured RAG source root does not exist.")
    if not root.is_dir():
        raise ValueError("Configured RAG source root is not a directory.")
    return root


def discover_sources(source_root: str | Path) -> list[Path]:
    """Scan only when explicitly called by an indexing or validation command."""
    root = validate_source_root(source_root)
    return [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix.lower() in ALLOWED_SUFFIXES and ".git" not in path.parts
    ]


def _read_source(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=2, ensure_ascii=False)
    if suffix == ".csv":
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            return "\n".join(",".join(row) for row in csv.reader(handle))
    return path.read_text(encoding="utf-8", errors="replace")


def load_document(path: str | Path, source_root: str | Path) -> SourceDocument:
    root = validate_source_root(source_root)
    resolved = Path(path).expanduser().resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError("RAG source path is outside the configured source root.")
    if resolved.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError("Unsupported RAG source suffix.")
    relative = resolved.relative_to(root).as_posix()
    text = _read_source(resolved)
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    document_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()
    category = relative.split("/", 1)[0] if "/" in relative else "uncategorized"
    return SourceDocument(
        document_id=document_id,
        source_path=str(resolved),
        relative_path=relative,
        title=resolved.stem.replace("-", " ").replace("_", " ").strip(),
        category=category,
        text=text,
        content_hash=content_hash,
        source_version=str(resolved.stat().st_mtime_ns),
    )
