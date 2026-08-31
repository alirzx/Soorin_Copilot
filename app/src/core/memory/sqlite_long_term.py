"""SQLite canonical long-term memory adapter for local development and tests."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import replace
from uuid import uuid4

from src.core.memory.long_term import (
    EpistemicStatus,
    LongTermMemoryRecord,
    MemoryPromotionPolicy,
    MemoryLifecycleAuditEvent,
    MemoryLifecycleResult,
    MemoryStatus,
    MemoryType,
    PromotionDecision,
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
, idempotency_fingerprint, logical_memory_key, has_unresolved_conflict,
policy_version
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
        logical_memory_key=row["logical_memory_key"] or "",
        has_unresolved_conflict=bool(row["has_unresolved_conflict"]),
        policy_version=row["policy_version"] or "ltm-promotion-v1",
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
    def _write_audit(
        connection: sqlite3.Connection,
        memory: LongTermMemoryRecord,
        *,
        action: str,
        from_status: str,
        to_status: str,
        reason_code: str,
        actor: str = "system",
        policy_version: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO local_long_term_memory_audit("
            "event_id,memory_id,logical_memory_key,idempotency_fingerprint,user_id,"
            "entity_ids_json,source_request_id,action,from_status,to_status,reason_code,"
            "policy_version,evidence_refs_json,actor,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"memaudit_{uuid4().hex}",
                memory.memory_id,
                memory.logical_memory_key,
                memory.idempotency_fingerprint,
                memory.user_id,
                json.dumps(memory.entity_ids, ensure_ascii=False, separators=(",", ":")),
                memory.source_request_id,
                action,
                from_status,
                to_status,
                reason_code,
                policy_version or memory.policy_version,
                json.dumps(memory.evidence_refs, ensure_ascii=False, separators=(",", ":")),
                actor,
                utc_now(),
            ),
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
            memory.logical_memory_key,
            int(memory.has_unresolved_conflict),
            memory.policy_version,
        )

    def get(
        self,
        *,
        user_id: str,
        memory_id: str,
        request_id: str = "",
        purpose: str = "",
    ) -> LongTermMemoryRecord | None:
        del request_id, purpose
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
                    if existing.status == "active" and existing.freshness() == "expired":
                        expired = replace(
                            existing,
                            status="expired",
                            index_status="pending",
                            updated_at=utc_now(),
                            revision=existing.revision + 1,
                        )
                        assignments = ", ".join(
                            f"{name.strip()} = ?" for name in _COLUMNS.split(",")[2:]
                        )
                        cursor = connection.execute(
                            f"UPDATE local_long_term_memories SET {assignments} "
                            "WHERE memory_id = ? AND user_id = ? AND revision = ?",
                            (
                                *self._values(expired)[2:],
                                expired.memory_id,
                                expired.user_id,
                                existing.revision,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise LocalPersistenceConflictError(
                                "Long-term memory revision is stale."
                            )
                        self._write_audit(
                            connection,
                            expired,
                            action="expired",
                            from_status="active",
                            to_status="expired",
                            reason_code="validity_elapsed_on_replay",
                        )
                    else:
                        self._write_audit(
                            connection,
                            existing,
                            action="candidate_deduplicated",
                            from_status=existing.status,
                            to_status=existing.status,
                            reason_code="exact_fingerprint_replay",
                        )
                        connection.commit()
                        logger.info(
                            "event=long_term_memory_write_deduplicated user_ref_set=true status=%s",
                            existing.status,
                        )
                        return existing
                self._enforce_admission_quota(connection, memory)
                connection.execute(
                    f"INSERT INTO local_long_term_memories({_COLUMNS}) VALUES ({','.join('?' for _ in range(22))})",
                    self._values(memory),
                )
                self._write_entities(connection, memory.memory_id, memory.entity_ids)
                self._write_audit(
                    connection,
                    memory,
                    action="candidate_created" if memory.status == "candidate" else "promoted",
                    from_status="none",
                    to_status=memory.status,
                    reason_code="canonical_record_created",
                )
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

    def apply_promotion(
        self,
        *,
        candidate: LongTermMemoryRecord,
        decision: PromotionDecision,
        actor: str = "system",
        request_id: str = "",
    ) -> MemoryLifecycleResult:
        """Atomically evaluate and apply one candidate lifecycle decision."""
        del request_id
        assignments = ", ".join(f"{name.strip()} = ?" for name in _COLUMNS.split(",")[2:])
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    f"SELECT {_COLUMNS} FROM local_long_term_memories WHERE memory_id = ?",
                    (candidate.memory_id,),
                ).fetchone()
                if row is None:
                    raise LocalPersistenceConflictError("Promotion candidate was not found.")
                current = _record(row, self._entities(connection, candidate.memory_id))
                if (
                    current.user_id != candidate.user_id
                    or current.entity_ids != candidate.entity_ids
                    or current.logical_memory_key != candidate.logical_memory_key
                    or current.idempotency_fingerprint != candidate.idempotency_fingerprint
                ):
                    raise LocalPersistenceOwnershipError(
                        "Promotion candidate scope validation failed."
                    )
                if current.status == "active" and decision.action == "auto_promote":
                    connection.commit()
                    return MemoryLifecycleResult(current, decision, deduplicated=True)
                if current.status != "candidate" or current.revision != candidate.revision:
                    raise LocalPersistenceConflictError("Promotion candidate is no longer eligible.")

                self._write_audit(
                    connection,
                    current,
                    action="promotion_evaluated",
                    from_status=current.status,
                    to_status=current.status,
                    reason_code=decision.reason_code,
                    actor=actor,
                    policy_version=decision.policy_version,
                )
                active_row = connection.execute(
                    f"SELECT {_COLUMNS} FROM local_long_term_memories "
                    "WHERE user_id = ? AND logical_memory_key = ? AND status = 'active' "
                    "AND memory_id != ? ORDER BY updated_at DESC LIMIT 1",
                    (current.user_id, current.logical_memory_key, current.memory_id),
                ).fetchone()
                active = (
                    _record(active_row, self._entities(connection, active_row["memory_id"]))
                    if active_row is not None
                    else None
                )

                if decision.action in {"keep_candidate", "requires_review"}:
                    conflict_count = 0
                    if decision.reason_code == "unresolved_material_conflict" and active is not None:
                        active = replace(
                            active,
                            has_unresolved_conflict=True,
                            revision=active.revision + 1,
                            updated_at=utc_now(),
                        )
                        connection.execute(
                            f"UPDATE local_long_term_memories SET {assignments} "
                            "WHERE memory_id = ? AND user_id = ? AND revision = ?",
                            (*self._values(active)[2:], active.memory_id, active.user_id, active.revision - 1),
                        )
                        conflict_count = 1
                    current = replace(current, policy_version=decision.policy_version)
                    self._write_audit(
                        connection,
                        current,
                        action="review_required" if decision.action == "requires_review" else "kept_candidate",
                        from_status="candidate",
                        to_status="candidate",
                        reason_code=decision.reason_code,
                        actor=actor,
                        policy_version=decision.policy_version,
                    )
                    connection.commit()
                    return MemoryLifecycleResult(
                        current,
                        decision,
                        previous_memory=active,
                        conflict_count=conflict_count,
                    )

                if decision.action == "reject":
                    rejected = replace(
                        current,
                        status="rejected",
                        policy_version=decision.policy_version,
                        revision=current.revision + 1,
                        updated_at=utc_now(),
                    )
                    connection.execute(
                        f"UPDATE local_long_term_memories SET {assignments} "
                        "WHERE memory_id = ? AND user_id = ? AND revision = ?",
                        (*self._values(rejected)[2:], current.memory_id, current.user_id, current.revision),
                    )
                    self._write_audit(
                        connection,
                        rejected,
                        action="rejected",
                        from_status="candidate",
                        to_status="rejected",
                        reason_code=decision.reason_code,
                        actor=actor,
                        policy_version=decision.policy_version,
                    )
                    connection.commit()
                    return MemoryLifecycleResult(rejected, decision)

                if decision.action != "auto_promote":
                    raise LocalPersistenceConflictError("Unsupported promotion decision.")

                if active is not None and active.statement == current.statement:
                    refreshed = replace(
                        active,
                        source_request_id=current.source_request_id,
                        source_conversation_id=current.source_conversation_id,
                        valid_from=current.valid_from,
                        updated_at=utc_now(),
                        revision=active.revision + 1,
                        has_unresolved_conflict=False,
                        policy_version=decision.policy_version,
                    )
                    connection.execute(
                        "DELETE FROM local_long_term_memories WHERE memory_id = ? AND status = 'candidate'",
                        (current.memory_id,),
                    )
                    connection.execute(
                        f"UPDATE local_long_term_memories SET {assignments} "
                        "WHERE memory_id = ? AND user_id = ? AND revision = ?",
                        (*self._values(refreshed)[2:], active.memory_id, active.user_id, active.revision),
                    )
                    self._write_audit(
                        connection,
                        current,
                        action="confirmed",
                        from_status="candidate",
                        to_status="active",
                        reason_code="same_value_revalidated",
                        actor=actor,
                        policy_version=decision.policy_version,
                    )
                    connection.commit()
                    return MemoryLifecycleResult(
                        refreshed,
                        decision,
                        previous_memory=active,
                        deduplicated=True,
                    )

                promoted = MemoryPromotionPolicy.promote(
                    replace(current, policy_version=decision.policy_version),
                    epistemic_status=decision.epistemic_status,
                    confidence=decision.confidence,
                    provenance_category=decision.provenance_category,
                )
                superseded_count = 0
                if active is not None:
                    superseded = replace(
                        active,
                        status="superseded",
                        index_status="pending",
                        has_unresolved_conflict=False,
                        revision=active.revision + 1,
                        updated_at=utc_now(),
                    )
                    promoted = replace(promoted, supersedes_memory_id=active.memory_id)
                    connection.execute(
                        f"UPDATE local_long_term_memories SET {assignments} "
                        "WHERE memory_id = ? AND user_id = ? AND revision = ?",
                        (*self._values(superseded)[2:], active.memory_id, active.user_id, active.revision),
                    )
                    self._write_audit(
                        connection,
                        superseded,
                        action="superseded",
                        from_status="active",
                        to_status="superseded",
                        reason_code="newer_compatible_value",
                        actor=actor,
                        policy_version=decision.policy_version,
                    )
                    active = superseded
                    superseded_count = 1
                else:
                    try:
                        self._enforce_admission_quota(connection, promoted)
                    except LocalPersistenceQuotaError:
                        quota_decision = replace(
                            decision,
                            action="keep_candidate",
                            reason_code="active_memory_quota_full",
                            reason="Active memory quota is full; authoritative records were preserved.",
                        )
                        self._write_audit(
                            connection,
                            current,
                            action="kept_candidate",
                            from_status="candidate",
                            to_status="candidate",
                            reason_code=quota_decision.reason_code,
                            actor=actor,
                            policy_version=quota_decision.policy_version,
                        )
                        connection.commit()
                        return MemoryLifecycleResult(current, quota_decision)
                connection.execute(
                    f"UPDATE local_long_term_memories SET {assignments} "
                    "WHERE memory_id = ? AND user_id = ? AND revision = ?",
                    (*self._values(promoted)[2:], current.memory_id, current.user_id, current.revision),
                )
                self._write_audit(
                    connection,
                    promoted,
                    action="promoted",
                    from_status="candidate",
                    to_status="active",
                    reason_code=decision.reason_code,
                    actor=actor,
                    policy_version=decision.policy_version,
                )
                connection.commit()
                return MemoryLifecycleResult(
                    promoted,
                    decision,
                    previous_memory=active,
                    superseded_count=superseded_count,
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise LocalPersistenceConflictError(
                    "Equivalent active memory already exists."
                ) from exc
            except Exception:
                connection.rollback()
                raise

    def list_audit_events(
        self,
        *,
        user_id: str,
        memory_id: str | None = None,
        limit: int = 100,
    ) -> tuple[MemoryLifecycleAuditEvent, ...]:
        user = _identifier(user_id, "user_id")
        bounded = max(1, min(500, int(limit)))
        clauses = ["user_id = ?"]
        parameters: list[object] = [user]
        if memory_id is not None:
            clauses.append("memory_id = ?")
            parameters.append(_identifier(memory_id, "memory_id"))
        parameters.append(bounded)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM local_long_term_memory_audit WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, event_id DESC LIMIT ?",
                tuple(parameters),
            ).fetchall()
        return tuple(
            MemoryLifecycleAuditEvent(
                event_id=row["event_id"],
                memory_id=row["memory_id"],
                logical_memory_key=row["logical_memory_key"],
                idempotency_fingerprint=row["idempotency_fingerprint"],
                user_id=row["user_id"],
                entity_ids=tuple(json.loads(row["entity_ids_json"])),
                source_request_id=row["source_request_id"],
                action=row["action"],
                from_status=row["from_status"],
                to_status=row["to_status"],
                reason_code=row["reason_code"],
                policy_version=row["policy_version"],
                evidence_refs=tuple(json.loads(row["evidence_refs_json"])),
                actor=row["actor"],
                created_at=row["created_at"],
            )
            for row in rows
        )

    def list(
        self,
        *,
        user_id: str,
        entity_ids: tuple[str, ...] = (),
        memory_types: tuple[MemoryType, ...] = (),
        statuses: tuple[MemoryStatus, ...] = ("active",),
        epistemic_statuses: tuple[EpistemicStatus, ...] = (),
        limit: int = 100,
        request_id: str = "",
        purpose: str = "",
    ) -> tuple[LongTermMemoryRecord, ...]:
        del request_id, purpose
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
        return self._transition_terminal_status(
            user_id=user_id,
            memory_id=memory_id,
            expected_revision=expected_revision,
            status="invalidated",
            action="invalidated",
            reason_code="explicit_invalidation",
        )

    def expire(
        self,
        *,
        user_id: str,
        memory_id: str,
        expected_revision: int,
    ) -> LongTermMemoryRecord:
        return self._transition_terminal_status(
            user_id=user_id,
            memory_id=memory_id,
            expected_revision=expected_revision,
            status="expired",
            action="expired",
            reason_code="retention_or_validity_expired",
        )

    def _transition_terminal_status(
        self,
        *,
        user_id: str,
        memory_id: str,
        expected_revision: int,
        status: MemoryStatus,
        action: str,
        reason_code: str,
    ) -> LongTermMemoryRecord:
        user = _identifier(user_id, "user_id")
        identifier = _identifier(memory_id, "memory_id")
        assignments = ", ".join(f"{name.strip()} = ?" for name in _COLUMNS.split(",")[2:])
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    f"SELECT {_COLUMNS} FROM local_long_term_memories WHERE memory_id = ?",
                    (identifier,),
                ).fetchone()
                if row is None or row["user_id"] != user:
                    raise LocalPersistenceOwnershipError(
                        "Long-term memory ownership validation failed."
                    )
                current = _record(row, self._entities(connection, identifier))
                if current.revision != expected_revision:
                    raise LocalPersistenceConflictError("Long-term memory revision is stale.")
                updated = replace(
                    current,
                    status=status,
                    index_status="pending",
                    has_unresolved_conflict=False,
                    revision=current.revision + 1,
                    updated_at=utc_now(),
                )
                connection.execute(
                    f"UPDATE local_long_term_memories SET {assignments} "
                    "WHERE memory_id = ? AND user_id = ? AND revision = ?",
                    (*self._values(updated)[2:], identifier, user, expected_revision),
                )
                self._write_audit(
                    connection,
                    updated,
                    action=action,
                    from_status=current.status,
                    to_status=status,
                    reason_code=reason_code,
                )
                connection.commit()
                return updated
            except Exception:
                connection.rollback()
                raise

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
                self._write_audit(
                    connection,
                    superseded,
                    action="superseded",
                    from_status=current.status,
                    to_status="superseded",
                    reason_code="explicit_replacement",
                )
                connection.execute(
                    "INSERT OR IGNORE INTO local_users(user_id, created_at) VALUES (?, ?)",
                    (replacement.user_id, replacement.created_at),
                )
                self._enforce_admission_quota(connection, replacement)
                connection.execute(
                    f"INSERT INTO local_long_term_memories({_COLUMNS}) VALUES ({','.join('?' for _ in range(22))})",
                    self._values(replacement),
                )
                self._write_entities(connection, replacement.memory_id, replacement.entity_ids)
                self._write_audit(
                    connection,
                    replacement,
                    action="promoted" if replacement.status == "active" else "candidate_created",
                    from_status="none",
                    to_status=replacement.status,
                    reason_code="explicit_replacement",
                )
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
