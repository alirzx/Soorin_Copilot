"""Small validated product API schemas used by graph ingestion."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _first_value(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if value:
            return str(value).strip()
    return ""


@dataclass(frozen=True)
class TopologyConnectionRecord:
    """Validated unique communication pair from the product topology endpoint."""

    src_ip: str
    dst_ip: str
    weight: int = 1

    @classmethod
    def from_product_record(cls, record: dict[str, Any]) -> "TopologyConnectionRecord | None":
        src_ip = _first_value(record, ("src_ip", "source_ip", "src", "source"))
        dst_ip = _first_value(record, ("dst_ip", "destination_ip", "dst", "destination"))
        if not src_ip or not dst_ip:
            return None

        raw_weight = record.get("weight") or record.get("count") or 1
        try:
            weight = max(1, int(raw_weight))
        except (TypeError, ValueError):
            weight = 1

        return cls(src_ip=src_ip, dst_ip=dst_ip, weight=weight)


@dataclass(frozen=True)
class ProductTopologyResponse:
    """Validated topology response plus fetch metadata."""

    raw_payload: Any
    records: list[TopologyConnectionRecord]
    endpoint_path: str
    status_code: int
    elapsed_seconds: float

    @property
    def raw_record_count(self) -> int:
        return len(_extract_records(self.raw_payload))

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        *,
        endpoint_path: str,
        status_code: int,
        elapsed_seconds: float,
    ) -> "ProductTopologyResponse":
        raw_records = _extract_records(payload)
        records = [
            validated
            for raw_record in raw_records
            if (validated := TopologyConnectionRecord.from_product_record(raw_record)) is not None
        ]
        logger.info(
            "event=product_topology_records_validated endpoint_path=%s raw_records=%s valid_records=%s",
            endpoint_path,
            len(raw_records),
            len(records),
        )
        return cls(
            raw_payload=payload,
            records=records,
            endpoint_path=endpoint_path,
            status_code=status_code,
            elapsed_seconds=elapsed_seconds,
        )
