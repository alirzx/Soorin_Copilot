"""Hardened Product LTM adapter for summary-search DTO compatibility."""

from __future__ import annotations

import logging
import threading
from typing import Any

from src.core.identity import normalize_identifier
from src.core.memory.persistence import LocalPersistenceOwnershipError
from src.core.memory.product import (
    ProductLongTermMemoryStore as BaseProductLongTermMemoryStore,
    ProductMemoryContractError,
    _normalize_memory_wire,
    _object,
)
from src.core.observability.metrics import get_metrics


logger = logging.getLogger(__name__)


def _log_contract_failure(
    *,
    request_id: str,
    purpose: str,
    phase: str,
    reason: str,
    memory_id: str = "",
) -> None:
    try:
        safe_memory_id = normalize_identifier(memory_id or None, field_name="memory_id") or ""
    except ValueError:
        safe_memory_id = "invalid_identifier"
    logger.error(
        "event=product_ltm_contract_failed request_id=%s operation=search purpose=%s phase=%s reason=%s memory_id=%s memory_id_present=%s hydration_attempted=%s payload_logged=false",
        request_id,
        purpose or "inventory",
        phase,
        reason,
        safe_memory_id,
        str(bool(safe_memory_id)).lower(),
        str(phase == "canonical_hydration").lower(),
    )


class ProductLongTermMemoryStore(BaseProductLongTermMemoryStore):
    """Keep strict canonical validation while accepting bounded search summaries."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._limitation_lock = threading.Lock()
        self._read_limitations: dict[tuple[str, str], tuple[str, ...]] = {}

    def _remember_read_limitations(
        self,
        request_id: str,
        purpose: str,
        limitations: list[str],
    ) -> None:
        if not request_id or not limitations:
            return
        with self._limitation_lock:
            self._read_limitations[(request_id, purpose)] = tuple(dict.fromkeys(limitations))

    def consume_read_limitations(
        self,
        request_id: str,
        purpose: str | None = None,
    ) -> tuple[str, ...]:
        with self._limitation_lock:
            keys = tuple(
                key for key in self._read_limitations
                if key[0] == request_id and (purpose is None or key[1] == purpose)
            )
            values = tuple(
                item for key in keys for item in self._read_limitations.pop(key, ())
            )
        return tuple(dict.fromkeys(values))

    def list(
        self,
        *,
        user_id: str,
        entity_ids=(),
        memory_types=(),
        statuses=("active",),
        epistemic_statuses=(),
        limit: int = 100,
        logical_memory_key: str = "",
        request_id: str = "",
        purpose: str = "inventory",
    ):
        owner = self._owner(user_id)
        body: dict[str, Any] = {
            "entityIds": list(entity_ids),
            "memoryTypes": list(memory_types),
            "statuses": list(statuses),
            "epistemicStatuses": list(epistemic_statuses),
            "limit": limit,
            "offset": 0,
        }
        if logical_memory_key:
            body["logicalMemoryKey"] = logical_memory_key
        result = _object(
            self.client.search_ltm(
                user_id=owner,
                body=body,
                request_id=request_id,
            )
        )
        records = next(
            (result[name] for name in ("records", "items", "memories") if name in result),
            None,
        )
        if not isinstance(records, list):
            _log_contract_failure(
                request_id=request_id,
                purpose=purpose,
                phase="search_envelope",
                reason="records_not_list",
            )
            raise ProductMemoryContractError(
                "Product memory search response was invalid.",
                reason_code="records_not_list",
                phase="search_envelope",
            )

        hydrated = []
        limitations: list[str] = []
        skipped_count = 0
        for record in records:
            try:
                summary = _normalize_memory_wire(record)
            except ProductMemoryContractError as exc:
                _log_contract_failure(
                    request_id=request_id,
                    purpose=purpose,
                    phase="search_summary",
                    reason=exc.reason_code,
                )
                limitations.append("malformed_product_memory_skipped")
                skipped_count += 1
                get_metrics().observe_memory_canonical_record("malformed_skipped")
                continue
            summary_owner = summary.get("userId")
            if summary_owner is not None and summary_owner != owner:
                get_metrics().observe_memory_canonical_record("security_rejected")
                raise LocalPersistenceOwnershipError(
                    "Product long-term memory owner did not match the transport owner."
                )
            try:
                hydrated.append(self._domain_record(record, domain_user_id=user_id))
                continue
            except LocalPersistenceOwnershipError:
                get_metrics().observe_memory_canonical_record("security_rejected")
                raise
            except ProductMemoryContractError:
                if summary_owner is not None and summary_owner != owner:
                    raise
                memory_id = summary.get("memoryId")
                if not isinstance(memory_id, str) or not memory_id:
                    _log_contract_failure(
                        request_id=request_id,
                        purpose=purpose,
                        phase="search_summary",
                        reason="missing_fields:memoryId",
                    )
                    limitations.append("malformed_product_memory_skipped")
                    skipped_count += 1
                    get_metrics().observe_memory_canonical_record("malformed_skipped")
                    continue
            try:
                hydrated.append(
                    self._hydrate(
                        user_id,
                        memory_id,
                        request_id=request_id,
                        purpose=f"{purpose}_canonical_hydration",
                    )
                )
            except ProductMemoryContractError as exc:
                _log_contract_failure(
                    request_id=request_id,
                    purpose=purpose,
                    phase="canonical_hydration",
                    reason=exc.reason_code,
                    memory_id=memory_id,
                )
                limitations.append("malformed_product_memory_skipped")
                skipped_count += 1
                get_metrics().observe_memory_canonical_record("malformed_skipped")
                continue
        self._remember_read_limitations(request_id, purpose, limitations)
        for _record in hydrated:
            get_metrics().observe_memory_canonical_record("valid")
        logger.info(
            "event=product_ltm_read_completed request_id=%s operation=search purpose=%s "
            "status=ok record_count=%s valid_record_count=%s skipped_record_count=%s "
            "canonical_hydration=true limitation_count=%s",
            request_id,
            purpose or "inventory",
            len(records),
            len(hydrated),
            skipped_count,
            len(set(limitations)),
        )
        return tuple(hydrated)
