"""Hardened Product LTM adapter for summary-search DTO compatibility."""

from __future__ import annotations

import logging
from typing import Any

from src.core.memory.persistence import LocalPersistenceOwnershipError
from src.core.memory.product import (
    ProductLongTermMemoryStore as BaseProductLongTermMemoryStore,
    ProductMemoryContractError,
    _normalize_memory_wire,
    _object,
)


logger = logging.getLogger(__name__)


class ProductLongTermMemoryStore(BaseProductLongTermMemoryStore):
    """Keep strict canonical validation while accepting bounded search summaries."""

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
            raise ProductMemoryContractError("Product memory search response was invalid.")

        hydrated = []
        for record in records:
            summary = _normalize_memory_wire(record)
            summary_owner = summary.get("userId")
            if summary_owner is not None and summary_owner != owner:
                raise LocalPersistenceOwnershipError(
                    "Product long-term memory owner did not match the transport owner."
                )
            try:
                hydrated.append(self._domain_record(record, domain_user_id=user_id))
                continue
            except (ProductMemoryContractError, LocalPersistenceOwnershipError):
                if summary_owner is not None and summary_owner != owner:
                    raise
                memory_id = summary.get("memoryId")
                if not isinstance(memory_id, str) or not memory_id:
                    raise ProductMemoryContractError(
                        "Product memory search summary omitted memoryId."
                    )
            hydrated.append(
                self._hydrate(
                    user_id,
                    memory_id,
                    request_id=request_id,
                    purpose=f"{purpose}_canonical_hydration",
                )
            )
        logger.info(
            "event=product_ltm_read_completed request_id=%s operation=search purpose=%s status=ok record_count=%s canonical_hydration=true",
            request_id,
            purpose or "inventory",
            len(hydrated),
        )
        return tuple(hydrated)
