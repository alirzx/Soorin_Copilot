"""SQLite canonical long-term memory adapter for local development and tests."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import replace

from src.core.memory.long_term import (
    EpistemicStatus,
    LongTermMemoryRecord,
    MemoryStatus,
    MemoryType,
    utc_now,
)
from src.core.memory.persistence import (
    LocalPersistenceConflictError,
    LocalPersistenceOwnershipError,
    LocalPersistenceQuotaError,
    MemoryStoragePolicy,
)
from src.core.memory.sqlite import LocalSQLiteDatabase, _identifier

logger = logging.getLogger(__name__)


_COLUMNS = """
memory_id, user_id, memory_type, statement, epistemic_status, confidence,
source_request_id, source_conversation_id, evidence_refs_json,
provenance_category, valid_from, valid_until, created_at, updated_at,
revision, status, index_status, supersedes_memory_id
, idempotency_fingerprint
"""


def _record(row: sqlite3.Row, entity_ids: tuple[str, ...]) -> LongTermMemoryRecord:
    return LongTermMemoryRecord(
        memory_id=row["memory_id"],
        memory_type=row["memory_type"],
        user_id=row["user_id"],
        entity_ids=entity_ids,
        statement=row["statement"],
        epistemic_status=row["epistemic_status"],
        confidence=float(row["confidence"]),
        source_request_id=row["source_request_id"],
        source_conversation_id=row["source_conversation_id"],
        evidence_refs=tuple(json.loads(row["evidence_refs_json"])),
        provenance_category=row["provenance_category"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        revision=int(row["revision"]),
        status=row["status"],
        index_status=row["index_status"],
        supersedes_memory_id=row["supersedes_memory_id"],
        idempotency_fingerprint=row["idempotency_fingerprint"] or "",
    )


class SQLiteLongTermMemoryStore:
    """Canonical local store with ownership and optimistic-revision checks."""

    def __init__(
        self,
        database: LocalSQLiteDatabase,
        policy: MemoryStoragePolicy | None = None,
    ) -> None:
        self.database = database
        self.policy = policy or MemoryStoragePolicy()

    def _enforce_admission_quota(
        self,
        connection: sqlite3.Connection,
        memory: LongTermMemoryRecord,
    ) -> None:
        if memory.status == "candidate":
            count = int(connection.execute(
                "SELECT COUNT(*) AS count FROM local_long_term_memories "
                "WHERE user_id = ? AND status = 'candidate'",
                (memory.user_id,),
            ).fetchone()["count"])
            if count >= self.policy.max_candidate_long_term_per_user:
                oldest = connection.execute(
                    "SELECT memory_id FROM local_long_term_memories "
                    "WHERE user_id = ? AND status = 'candidate' "
                    "ORDER BY updated_at ASC, memory_id ASC LIMIT 1",
                    (memory.user_id,),
                ).fetchone()
                if oldest is not None:
                    connection.execute(
                        "DELETE FROM local_long_term_memories WHERE memory_id = ?",
                        (oldest["memory_id"],),
                    )
                    logger.info(
                        "event=long_term_candidate_quota_eviction user_ref_set=true limit=%s",
                        self.policy.max_candidate_long_term_per_user,
                    )
        elif memory.status == "active":
            count = int(connection.execute(
                "SELECT COUNT(*) AS count FROM local_long_term_memories "
                "WHERE user_id = ? AND status = 'active'",
                (memory.user_id,),
            ).fetchone()["count"])
            if count >= self.policy.max_active_long_term_per_user:
                logger.warning(
                    "event=long_term_active_quota_rejected user_ref_set=true limit=%s",
                    self.policy.max_active_long_term_per_user,
                )
                raise LocalPersistenceQuotaError(
                    "Active long-term memory quota is full; authoritative records were preserved."
                )

    @staticmethod
    def _entities(connection: sqlite3.Connection, memory_id: str) -> tuple[str, ...]:
        rows = connection.execute(
            "SELECT entity_id FROM local_long_term_memory_entities "
            "WHERE memory_id = ? ORDER BY entity_id",
            (memory_id,),
        ).fetchall()
        return tuple(row["entity_id"] for row in rows)

    @staticmethod
    def _write_entities(
        connection: sqlite3.Connection,
        memory_id: str,
        entity_ids: tuple[str, ...],
    ) -> None:
        connection.execute(
            "DELETE FROM local_long_term_memory_entities WHERE memory_id = ?",
            (memory_id,),
        )
        connection.executemany(
            "INSERT INTO local_long_term_memory_entities(memory_id, entity_id) VALUES (?, ?)",
            ((memory_id, entity_id) for entity_id in entity_ids),
        )

    @staticmethod
    def _values(memory: LongTermMemoryRecord) -> tuple[object, ...]:
        return (
            memory.memory_id,
            memory.user_id,
            memory.memory_type,
            memory.statement,
            memory.epistemic_status,
            memory.confidence,
            memory.source_request_id,
            memory.source_conversation_id,
            json.dumps(memory.evidence_refs, ensure_ascii=False, separators=(",", ":")),
            memory.provenance_category,
            memory.valid_from,
            memory.valid_until,
            memory.created_at,
            memory.updated_at,
            memory.revision,
            memory.status,
            memory.index_status,
            memory.supersedes_memory_id,
            memory.idempotency_fingerprint,
        )

    def get(self, *, user_id: str, memory_id: str) -> LongTermMemoryRecord | None:
        user = _identifier(user_id, "user_id")
        identifier = _identifier(memory_id, "memory_id")
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM local_long_term_memories WHERE memory_id = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                return None
            if row["user_id"] != user:
                raise LocalPersistenceOwnershipError("Long-term memory ownership validation failed.")
            return _record(row, self._entities(connection, identifier))

    def put(self, *, memory: LongTermMemoryRecord) -> LongTermMemoryRecord:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT OR IGNORE INTO local_users(user_id, created_at) VALUES (?, ?)",
                    (memory.user_id, memory.created_at),
                )
                equivalent = connection.execute(
                    f"SELECT {_COLUMNS} FROM local_long_term_memories "
                    "WHERE user_id = ? AND idempotency_fingerprint = ? "
                    "AND status IN ('candidate', 'active') ORDER BY updated_at DESC LIMIT 1",
                    (memory.user_id, memory.idempotency_fingerprint),
                ).fetchone()
                if equivalent is not None:
                    existing = _record(
                        equivalent,
                        self._entities(connection, equivalent["memory_id"]),
                    )
                    connection.commit()
                    logger.info(
                        "event=long_term_memory_write_deduplicated user_ref_set=true status=%s",
                        existing.status,
                    )
                    return existing
                self._enforce_admission_quota(connection, memory)
                connection.execute(
                    f"INSERT INTO local_long_term_memories({_COLUMNS}) VALUES ({','.join('?' for _ in range(19))})",
                    self._values(memory),
                )
                self._write_entities(connection, memory.memory_id, memory.entity_ids)
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise LocalPersistenceConflictError("Long-term memory already exists.") from exc
            except Exception:
                connection.rollback()
                raise
        return memory

    def update(
        self,
        *,
        memory: LongTermMemoryRecord,
        expected_revision: int,
    ) -> LongTermMemoryRecord:
        if memory.revision != expected_revision + 1:
            raise LocalPersistenceConflictError("Long-term memory revision is invalid.")
        assignments = ", ".join(f"{name.strip()} = ?" for name in _COLUMNS.split(",")[2:])
        values = self._values(memory)[2:]
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT user_id, status FROM local_long_term_memories WHERE memory_id = ?",
                    (memory.memory_id,),
                ).fetchone()
                if row is None or row["user_id"] != memory.user_id:
                    raise LocalPersistenceOwnershipError("Long-term memory ownership validation failed.")
                if row["status"] != "active" and memory.status == "active":
                    self._enforce_admission_quota(connection, memory)
                cursor = connection.execute(
                    f"UPDATE local_long_term_memories SET {assignments} "
                    "WHERE memory_id = ? AND user_id = ? AND revision = ?",
                    (*values, memory.memory_id, memory.user_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise LocalPersistenceConflictError("Long-term memory revision is stale.")
                self._write_entities(connection, memory.memory_id, memory.entity_ids)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return memory

    def list(
        self,
        *,
        user_id: str,
        entity_ids: tuple[str, ...] = (),
        memory_types: tuple[MemoryType, ...] = (),
        statuses: tuple[MemoryStatus, ...] = ("active",),
        epistemic_statuses: tuple[EpistemicStatus, ...] = (),
        limit: int = 100,
    ) -> tuple[LongTermMemoryRecord, ...]:
        user = _identifier(user_id, "user_id")
        bounded_limit = max(1, min(500, int(limit)))
        clauses = ["m.user_id = ?"]
        parameters: list[object] = [user]
        for column, values in (
            ("m.memory_type", memory_types),
            ("m.status", statuses),
            ("m.epistemic_status", epistemic_statuses),
        ):
            if values:
                clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
                parameters.extend(values)
        if entity_ids:
            clauses.append(
                "EXISTS (SELECT 1 FROM local_long_term_memory_entities e "
                f"WHERE e.memory_id = m.memory_id AND e.entity_id IN ({','.join('?' for _ in entity_ids)}))"
            )
            parameters.extend(entity_ids)
        parameters.append(bounded_limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM local_long_term_memories m "
                f"WHERE {' AND '.join(clauses)} ORDER BY m.updated_at DESC, m.memory_id LIMIT ?",
                tuple(parameters),
            ).fetchall()
            return tuple(_record(row, self._entities(connection, row["memory_id"])) for row in rows)

    def invalidate(
        self,
        *,
        user_id: str,
        memory_id: str,
        expected_revision: int,
    ) -> LongTermMemoryRecord:
        current = self.get(user_id=user_id, memory_id=memory_id)
        if current is None:
            raise LocalPersistenceConflictError("Long-term memory was not found.")
        updated = replace(
            current,
            status="invalidated",
            index_status="pending",
            revision=current.revision + 1,
            updated_at=utc_now(),
        )
        return self.update(memory=updated, expected_revision=expected_revision)

    def supersede(
        self,
        *,
        user_id: str,
        memory_id: str,
        replacement: LongTermMemoryRecord,
        expected_revision: int,
    ) -> tuple[LongTermMemoryRecord, LongTermMemoryRecord]:
        user = _identifier(user_id, "user_id")
        identifier = _identifier(memory_id, "memory_id")
        if replacement.user_id != user:
            raise LocalPersistenceOwnershipError("Long-term memory ownership validation failed.")
        assignments = ", ".join(f"{name.strip()} = ?" for name in _COLUMNS.split(",")[2:])
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    f"SELECT {_COLUMNS} FROM local_long_term_memories WHERE memory_id = ?",
                    (identifier,),
                ).fetchone()
                if row is None or row["user_id"] != user:
                    raise LocalPersistenceOwnershipError("Long-term memory ownership validation failed.")
                current = _record(row, self._entities(connection, identifier))
                if current.revision != expected_revision:
                    raise LocalPersistenceConflictError("Long-term memory revision is stale.")
                superseded = replace(
                    current,
                    status="superseded",
                    index_status="pending",
                    revision=current.revision + 1,
                    updated_at=utc_now(),
                )
                replacement = replace(replacement, supersedes_memory_id=current.memory_id)
                connection.execute(
                    f"UPDATE local_long_term_memories SET {assignments} "
                    "WHERE memory_id = ? AND user_id = ? AND revision = ?",
                    (*self._values(superseded)[2:], identifier, user, expected_revision),
                )
                self._write_entities(connection, identifier, superseded.entity_ids)
                connection.execute(
                    "INSERT OR IGNORE INTO local_users(user_id, created_at) VALUES (?, ?)",
                    (replacement.user_id, replacement.created_at),
                )
                self._enforce_admission_quota(connection, replacement)
                connection.execute(
                    f"INSERT INTO local_long_term_memories({_COLUMNS}) VALUES ({','.join('?' for _ in range(19))})",
                    self._values(replacement),
                )
                self._write_entities(connection, replacement.memory_id, replacement.entity_ids)
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise LocalPersistenceConflictError("Replacement memory already exists.") from exc
            except Exception:
                connection.rollback()
                raise
        return superseded, replacement

    def delete(self, *, user_id: str, memory_id: str) -> bool:
        current = self.get(user_id=user_id, memory_id=memory_id)
        if current is None:
            return False
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "DELETE FROM local_long_term_memories WHERE memory_id = ? AND user_id = ?",
                    (memory_id, user_id),
                )
                connection.commit()
                return cursor.rowcount == 1
            except Exception:
                connection.rollback()
                raise
