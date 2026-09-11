"""Hardened Product LTM adapter for summary-search DTO compatibility."""

from __future__ import annotations

from typing import Any

from src.core.memory.product import (
    ProductLongTermMemoryStore as BaseProductLongTermMemoryStore,
    ProductMemoryContractError,
    _normalize_memory_wire,
    _object,
)


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
                user_id=self._owner(user_id),
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
            try:
                hydrated.append(self._domain_record(record, domain_user_id=user_id))
                continue
            except ProductMemoryContractError:
                summary = _normalize_memory_wire(record)
                memory_id = summary.get("memoryId")
                if not isinstance(memory_id, str) or not memory_id:
                    raise
            hydrated.append(
                self._hydrate(
                    user_id,
                    memory_id,
                    request_id=request_id,
                    purpose=f"{purpose}_canonical_hydration",
                )
            )
        return tuple(hydrated)
