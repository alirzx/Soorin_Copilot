"""Canonical typed long-term memory and deterministic authority policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Literal, get_args
from uuid import uuid4

from src.core.context.models import approx_tokens, compact_preview
from src.core.identity import normalize_identifier


MemoryType = Literal[
    "validated_finding",
    "investigation_outcome",
    "analyst_correction",
    "approved_asset_fact",
    "known_benign_behavior",
    "hypothesis_resolution",
]
EpistemicStatus = Literal[
    "candidate",
    "analyst_confirmed",
    "source_validated",
    "historical",
    "unconfirmed",
]
MemoryStatus = Literal["candidate", "active", "invalidated", "superseded", "rejected", "expired"]
IndexStatus = Literal["synced", "pending", "stale", "failed", "not_indexed"]
FreshnessStatus = Literal["current", "historical", "expired", "inactive"]
PromotionAction = Literal["auto_promote", "keep_candidate", "requires_review", "reject"]
LifecycleAction = Literal[
    "candidate_created",
    "candidate_deduplicated",
    "promotion_evaluated",
    "promoted",
    "confirmed",
    "kept_candidate",
    "review_required",
    "rejected",
    "superseded",
    "invalidated",
    "expired",
]

MEMORY_TYPES = frozenset(get_args(MemoryType))
EPISTEMIC_STATUSES = frozenset(get_args(EpistemicStatus))
MEMORY_STATUSES = frozenset(get_args(MemoryStatus))
INDEX_STATUSES = frozenset(get_args(IndexStatus))
AUTHORITATIVE_EPISTEMIC = frozenset({"analyst_confirmed", "source_validated"})
MAX_MEMORY_STATEMENT_CHARS = 2_000
MAX_RETRIEVAL_TEXT_CHARS = 2_400
MAX_MEMORY_ENTITIES = 8
MAX_EVIDENCE_REFS = 20
SECRET_PATTERN = re.compile(
    r"(?i)(?:authorization\s*:|bearer\s+[a-z0-9._-]{16,}|"
    r"api[_ -]?key\s*[=:]\s*\S+|password\s*[=:]\s*\S+)"
)
PROMOTION_POLICY_VERSION = "ltm-promotion-v1"
AUTO_PROMOTION_CAPABILITIES = frozenset({"asset.get_profile", "asset.get_detection"})
AUTO_PROMOTION_EVIDENCE_CLASSES = frozenset({
    "asset_identity",
    "asset_role",
    "asset_identity_protocols",
    "detection_classification",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: str, field_name: str, *, optional: bool = False) -> str | None:
    text = str(value or "").strip()
    if optional and not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _bounded_identifier(value: str, field_name: str) -> str:
    return normalize_identifier(value, field_name=field_name) or ""


def _bounded_identifiers(values: tuple[str, ...], field_name: str, maximum: int) -> tuple[str, ...]:
    if len(values) > maximum:
        raise ValueError(f"{field_name} exceeds its item limit")
    normalized: list[str] = []
    for raw in values:
        item = _bounded_identifier(raw, field_name)
        if item not in normalized:
            normalized.append(item)
    return tuple(normalized)


def _structured_statement(statement: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(statement)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def logical_memory_key_for(
    *,
    user_id: str,
    entity_ids: tuple[str, ...],
    memory_type: MemoryType,
    statement: str,
    evidence_refs: tuple[str, ...],
) -> str:
    """Identify one operational fact family independently of its current value."""
    structured = _structured_statement(statement)
    payload = structured or {}
    evidence_classes = tuple(sorted(
        str(item)
        for item in payload.get("evidence_classes", ())
        if str(item).strip()
    )) or tuple(sorted(
        ref.removeprefix("evidence_class_")
        for ref in evidence_refs
        if ref.startswith("evidence_class_")
    ))
    identity = {
        "user_id": user_id,
        "entity_ids": sorted(entity_ids),
        "memory_type": memory_type,
        "capability": str(payload.get("source_capability") or ""),
        "evidence_classes": evidence_classes,
        "views": sorted(str(item) for item in payload.get("selected_views", ())),
        "schema_version": str(payload.get("schema_version") or ""),
        "fact_identity": "" if structured else hashlib.sha256(statement.encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class PromotionDecision:
    action: PromotionAction
    reason_code: str
    reason: str
    confidence: float = 0.0
    quality_score: float = 0.0
    evidence_refs: tuple[str, ...] = ()
    policy_version: str = PROMOTION_POLICY_VERSION
    epistemic_status: EpistemicStatus = "candidate"
    provenance_category: str = "investigation"


@dataclass(frozen=True)
class MemoryLifecycleAuditEvent:
    event_id: str
    memory_id: str
    logical_memory_key: str
    idempotency_fingerprint: str
    user_id: str
    entity_ids: tuple[str, ...]
    source_request_id: str
    action: LifecycleAction
    from_status: str
    to_status: str
    reason_code: str
    policy_version: str
    evidence_refs: tuple[str, ...]
    actor: str
    created_at: str


@dataclass(frozen=True)
class MemoryLifecycleResult:
    memory: "LongTermMemoryRecord"
    decision: PromotionDecision
    previous_memory: "LongTermMemoryRecord | None" = None
    deduplicated: bool = False
    superseded_count: int = 0
    conflict_count: int = 0


@dataclass(frozen=True)
class LongTermMemoryRecord:
    memory_id: str
    memory_type: MemoryType
    user_id: str
    entity_ids: tuple[str, ...]
    statement: str
    epistemic_status: EpistemicStatus
    confidence: float
    source_request_id: str
    source_conversation_id: str
    evidence_refs: tuple[str, ...] = ()
    provenance_category: str = "investigation"
    valid_from: str = ""
    valid_until: str | None = None
    created_at: str = ""
    updated_at: str = ""
    revision: int = 1
    status: MemoryStatus = "candidate"
    index_status: IndexStatus = "not_indexed"
    supersedes_memory_id: str | None = None
    idempotency_fingerprint: str = ""
    logical_memory_key: str = ""
    has_unresolved_conflict: bool = False
    policy_version: str = PROMOTION_POLICY_VERSION

    def __post_init__(self) -> None:
        now = utc_now()
        object.__setattr__(self, "memory_id", _bounded_identifier(self.memory_id, "memory_id"))
        object.__setattr__(self, "user_id", _bounded_identifier(self.user_id, "user_id"))
        object.__setattr__(
            self,
            "source_request_id",
            _bounded_identifier(self.source_request_id, "source_request_id"),
        )
        object.__setattr__(
            self,
            "source_conversation_id",
            _bounded_identifier(self.source_conversation_id, "source_conversation_id"),
        )
        if self.memory_type not in MEMORY_TYPES:
            raise ValueError("Unsupported long-term memory type")
        if self.epistemic_status not in EPISTEMIC_STATUSES:
            raise ValueError("Unsupported epistemic status")
        if self.status not in MEMORY_STATUSES:
            raise ValueError("Unsupported memory status")
        if self.index_status not in INDEX_STATUSES:
            raise ValueError("Unsupported memory index status")
        statement = str(self.statement or "").strip()
        if not statement or len(statement) > MAX_MEMORY_STATEMENT_CHARS:
            raise ValueError("Memory statement is blank or oversized")
        if SECRET_PATTERN.search(statement):
            raise ValueError("Memory statement contains credential-shaped text")
        object.__setattr__(self, "statement", statement)
        object.__setattr__(
            self,
            "entity_ids",
            _bounded_identifiers(tuple(self.entity_ids), "entity_id", MAX_MEMORY_ENTITIES),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _bounded_identifiers(tuple(self.evidence_refs), "evidence_ref", MAX_EVIDENCE_REFS),
        )
        provenance = str(self.provenance_category or "").strip().lower()
        if provenance not in {"analyst", "product", "investigation"}:
            raise ValueError("Unsupported memory provenance category")
        object.__setattr__(self, "provenance_category", provenance)
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("Memory confidence must be between zero and one")
        object.__setattr__(self, "confidence", confidence)
        if not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("Memory revision must be positive")
        object.__setattr__(self, "valid_from", _timestamp(self.valid_from or now, "valid_from") or now)
        object.__setattr__(self, "valid_until", _timestamp(self.valid_until or "", "valid_until", optional=True))
        object.__setattr__(self, "created_at", _timestamp(self.created_at or now, "created_at") or now)
        object.__setattr__(self, "updated_at", _timestamp(self.updated_at or now, "updated_at") or now)
        if self.supersedes_memory_id:
            object.__setattr__(
                self,
                "supersedes_memory_id",
                _bounded_identifier(self.supersedes_memory_id, "supersedes_memory_id"),
            )
        if self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        if self.status == "active" and self.epistemic_status not in AUTHORITATIVE_EPISTEMIC | {"historical"}:
            raise ValueError("Active memory requires authoritative or historical epistemic status")
        fingerprint_payload = {
            "user_id": self.user_id,
            "memory_type": self.memory_type,
            "entity_ids": sorted(self.entity_ids),
            "statement": self.statement,
            "evidence_refs": sorted(self.evidence_refs),
        }
        expected_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        supplied = str(self.idempotency_fingerprint or "").strip().lower()
        if supplied and supplied != expected_fingerprint:
            raise ValueError("Long-term memory idempotency fingerprint is invalid")
        object.__setattr__(self, "idempotency_fingerprint", expected_fingerprint)
        expected_logical_key = logical_memory_key_for(
            user_id=self.user_id,
            entity_ids=self.entity_ids,
            memory_type=self.memory_type,
            statement=self.statement,
            evidence_refs=self.evidence_refs,
        )
        supplied_key = str(self.logical_memory_key or "").strip().lower()
        if supplied_key and supplied_key != expected_logical_key:
            raise ValueError("Long-term memory logical key is invalid")
        object.__setattr__(self, "logical_memory_key", expected_logical_key)
        object.__setattr__(self, "has_unresolved_conflict", bool(self.has_unresolved_conflict))
        policy_version = str(self.policy_version or PROMOTION_POLICY_VERSION).strip()
        if not policy_version or len(policy_version) > 64:
            raise ValueError("Long-term memory policy version is invalid")
        object.__setattr__(self, "policy_version", policy_version)

    @classmethod
    def candidate(
        cls,
        *,
        memory_type: MemoryType,
        user_id: str,
        entity_ids: tuple[str, ...],
        statement: str,
        source_request_id: str,
        source_conversation_id: str,
        evidence_refs: tuple[str, ...] = (),
        provenance_category: str = "investigation",
        confidence: float = 0.0,
    ) -> "LongTermMemoryRecord":
        return cls(
            memory_id=f"mem_{uuid4().hex}",
            memory_type=memory_type,
            user_id=user_id,
            entity_ids=entity_ids,
            statement=statement,
            epistemic_status="candidate",
            confidence=confidence,
            source_request_id=source_request_id,
            source_conversation_id=source_conversation_id,
            evidence_refs=evidence_refs,
            provenance_category=provenance_category,
        )

    @property
    def authoritative(self) -> bool:
        return self.status == "active" and self.epistemic_status in AUTHORITATIVE_EPISTEMIC

    def freshness(self, *, now: datetime | None = None) -> FreshnessStatus:
        if self.status != "active":
            return "inactive"
        if self.memory_type == "investigation_outcome" or self.epistemic_status == "historical":
            return "historical"
        current = now or datetime.now(timezone.utc)
        if self.valid_until and datetime.fromisoformat(self.valid_until) < current:
            return "expired"
        return "current"


class MemoryPromotionPolicy:
    """Deterministic authority transitions; no model output can approve itself."""

    @staticmethod
    def evaluate_candidate_for_promotion(
        candidate: LongTermMemoryRecord,
        evidence: Any,
        *,
        enabled: bool = True,
        policy_version: str = PROMOTION_POLICY_VERSION,
    ) -> PromotionDecision:
        refs = tuple(candidate.evidence_refs)
        base = {
            "evidence_refs": refs,
            "policy_version": policy_version,
        }
        if candidate.status != "candidate" or candidate.epistemic_status != "candidate":
            return PromotionDecision("reject", "candidate_status_ineligible", "Record is not an eligible candidate.", **base)
        if not candidate.user_id or len(candidate.entity_ids) != 1:
            return PromotionDecision("reject", "owner_or_entity_invalid", "Exact owner and single-entity scope are required.", **base)
        statement = _structured_statement(candidate.statement)
        if statement is None:
            action: PromotionAction = "requires_review" if candidate.provenance_category == "analyst" else "reject"
            return PromotionDecision(action, "structured_evidence_required", "Automatic authority requires structured evidence.", **base)
        if candidate.memory_type in {"hypothesis_resolution", "investigation_outcome"}:
            return PromotionDecision("requires_review", "memory_type_requires_review", "This memory type requires explicit review.", **base)
        if candidate.provenance_category == "analyst":
            return PromotionDecision("requires_review", "analyst_confirmation_required", "Analyst-provided notes are not operational evidence.", **base)
        if not enabled:
            return PromotionDecision("keep_candidate", "auto_promotion_disabled", "Automatic promotion is disabled.", **base)
        capability = str(statement.get("source_capability") or "")
        evidence_classes = frozenset(str(item) for item in statement.get("evidence_classes", ()))
        views = tuple(str(item) for item in statement.get("selected_views", ()))
        schema_version = str(statement.get("schema_version") or "")
        if capability not in AUTO_PROMOTION_CAPABILITIES:
            return PromotionDecision("keep_candidate", "source_not_auto_promotable", "The source capability is not auto-promotable.", **base)
        if not evidence_classes or not evidence_classes.issubset(AUTO_PROMOTION_EVIDENCE_CLASSES):
            return PromotionDecision("requires_review", "evidence_class_requires_review", "The evidence class is not safe for automatic authority.", **base)
        if not views or not schema_version or statement.get("completeness") != "complete" or not statement.get("projection_complete"):
            return PromotionDecision("keep_candidate", "projection_incomplete", "A complete compatible projection is required.", **base)
        if (
            getattr(evidence, "source_capability", "") != capability
            or tuple(getattr(evidence, "entities", ())) != candidate.entity_ids
            or getattr(evidence, "status", "") != "ok"
            or getattr(evidence, "completeness", "") != "complete"
            or getattr(evidence, "freshness", "") != "current"
            or bool(getattr(evidence, "truncated", False))
            or bool(getattr(evidence, "projection_truncated", False))
            or not bool(getattr(evidence, "source_payload_complete", False))
            or not bool(getattr(evidence, "projection_usable", False))
            or tuple(getattr(evidence, "selected_views", ())) != views
            or str(getattr(evidence, "projection_schema_version", "")) != schema_version
            or getattr(evidence, "view_payload", None) != statement.get("evidence")
        ):
            return PromotionDecision("keep_candidate", "validated_tool_result_required", "Validated complete ToolResult evidence is required.", **base)
        if tuple(getattr(evidence, "contradictions", ())):
            return PromotionDecision("requires_review", "unresolved_material_conflict", "Material contradictions require review.", **base)
        if not str(getattr(evidence, "retrieved_at", "") or getattr(evidence, "valid_at", "")):
            return PromotionDecision("keep_candidate", "freshness_metadata_required", "Freshness metadata is required.", **base)
        return PromotionDecision(
            "auto_promote",
            "validated_product_fact",
            "Complete Product-backed structured fact is eligible for automatic promotion.",
            confidence=0.95,
            quality_score=1.0,
            epistemic_status="source_validated",
            provenance_category="product",
            **base,
        )

    @staticmethod
    def promote(
        candidate: LongTermMemoryRecord,
        *,
        epistemic_status: EpistemicStatus,
        confidence: float,
        provenance_category: str,
    ) -> LongTermMemoryRecord:
        if candidate.status != "candidate":
            raise ValueError("Only candidate memories can be promoted")
        allowed = epistemic_status in AUTHORITATIVE_EPISTEMIC
        if candidate.memory_type == "investigation_outcome":
            allowed = epistemic_status in {*AUTHORITATIVE_EPISTEMIC, "historical"}
        if not allowed:
            raise ValueError("The requested epistemic status cannot promote this memory")
        if candidate.memory_type in {"analyst_correction", "known_benign_behavior"} and (
            epistemic_status != "analyst_confirmed" and provenance_category != "product"
        ):
            raise ValueError("This memory type requires analyst or Product validation")
        status: MemoryStatus = "active"
        return replace(
            candidate,
            epistemic_status=epistemic_status,
            confidence=confidence,
            provenance_category=provenance_category,
            status=status,
            index_status="pending",
            revision=candidate.revision + 1,
            updated_at=utc_now(),
        )


@dataclass(frozen=True)
class MemoryRetrievalDocument:
    memory_id: str
    text: str
    content_hash: str


def retrieval_document(memory: LongTermMemoryRecord) -> MemoryRetrievalDocument:
    """Create one bounded atomic embedding projection; never generic-chunk memory."""
    import hashlib

    entities = ", ".join(memory.entity_ids) if memory.entity_ids else "none"
    text = (
        f"Memory type: {memory.memory_type}. Entities: {entities}. "
        f"Statement: {memory.statement}. Epistemic status: {memory.epistemic_status}. "
        f"Provenance: {memory.provenance_category}."
    )
    text = compact_preview(text, limit=MAX_RETRIEVAL_TEXT_CHARS)
    return MemoryRetrievalDocument(
        memory_id=memory.memory_id,
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


@dataclass(frozen=True)
class RetrievedLongTermMemory:
    memory: LongTermMemoryRecord
    relevance_score: float
    retrieval_reason: str
    freshness: FreshnessStatus

    @property
    def estimated_tokens(self) -> int:
        return approx_tokens(self.memory.statement) + 24

    def model_text(self) -> str:
        historical = "historical context" if self.freshness == "historical" else self.freshness
        entities = ", ".join(self.memory.entity_ids) or "not entity-specific"
        return (
            f"- type={self.memory.memory_type}; epistemic={self.memory.epistemic_status}; "
            f"freshness={historical}; provenance={self.memory.provenance_category}; "
            f"entities={entities}; statement={self.memory.statement}"
        )
