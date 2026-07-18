"""Deterministic text chunking with stable document and chunk identities."""

from __future__ import annotations

import hashlib
import re
from uuid import NAMESPACE_URL, uuid5
from dataclasses import dataclass

from src.core.rag.sources import SourceDocument


HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class SourceChunk:
    chunk_id: str
    document_id: str
    text: str
    source_path: str
    relative_path: str
    section: str
    category: str
    title: str
    content_hash: str
    source_version: str


def chunk_document(
    document: SourceDocument,
    *,
    chunk_size: int = 1200,
    overlap: int = 150,
) -> list[SourceChunk]:
    size = max(200, int(chunk_size))
    overlap = min(max(0, int(overlap)), size - 1)
    step = size - overlap
    chunks: list[SourceChunk] = []
    section = document.title
    headings = [(match.start(), match.group(1).strip()) for match in HEADING.finditer(document.text)]
    for index, start in enumerate(range(0, len(document.text), step)):
        text = document.text[start : start + size].strip()
        if not text:
            continue
        preceding = [title for position, title in headings if position <= start]
        if preceding:
            section = preceding[-1]
        chunk_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        chunk_id = str(uuid5(NAMESPACE_URL, f"{document.document_id}:{index}:{chunk_hash}"))
        chunks.append(
            SourceChunk(
                chunk_id=chunk_id,
                document_id=document.document_id,
                text=text,
                source_path=document.source_path,
                relative_path=document.relative_path,
                section=section,
                category=document.category,
                title=document.title,
                content_hash=chunk_hash,
                source_version=document.source_version,
            )
        )
    return chunks
