"""Deterministic entity resolution for baseline Copilot context routing."""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any

from src.core.context.models import EntityResolution, ResolvedEntity, compact_preview


logger = logging.getLogger(__name__)
IPV4_CANDIDATE_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _valid_ipv4(value: str | None) -> str | None:
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if ip.version != 4:
        return None
    return str(ip)


class EntityResolver:
    """Resolve only IPv4 entities for the frozen baseline."""

    def resolve(
        self,
        message: str,
        ui_context: dict[str, Any] | None = None,
        *,
        request_id: str = "",
    ) -> EntityResolution:
        logger.info(
            "event=entity_resolver_start request_id=%s message_preview=%r ui_context_present=%s",
            request_id,
            compact_preview(message),
            bool(ui_context),
        )

        message_ips: list[str] = []
        seen: set[str] = set()
        for candidate in IPV4_CANDIDATE_RE.findall(message or ""):
            ip = _valid_ipv4(candidate)
            if ip and ip not in seen:
                message_ips.append(ip)
                seen.add(ip)

        ui_selected_ip = _valid_ipv4(str((ui_context or {}).get("selected_ip") or ""))

        if len(message_ips) > 1:
            entities = [ResolvedEntity(type="ip", value=ip, source="message") for ip in message_ips]
            resolution = EntityResolution(
                status="ambiguous",
                entities=entities,
                primary_entity=None,
                candidate_count=len(message_ips),
            )
        elif len(message_ips) == 1:
            primary = ResolvedEntity(type="ip", value=message_ips[0], source="message")
            resolution = EntityResolution(
                status="resolved",
                entities=[primary],
                primary_entity=primary,
                candidate_count=1,
            )
        elif ui_selected_ip:
            primary = ResolvedEntity(type="ip", value=ui_selected_ip, source="ui")
            resolution = EntityResolution(
                status="resolved",
                entities=[primary],
                primary_entity=primary,
                candidate_count=1,
            )
        else:
            resolution = EntityResolution(status="none", entities=[], primary_entity=None, candidate_count=0)

        logger.info(
            "event=entity_resolver_complete request_id=%s status=%s entity_count=%s primary=%s source=%s",
            request_id,
            resolution.status,
            len(resolution.entities),
            resolution.primary_entity.value if resolution.primary_entity else "",
            resolution.primary_entity.source if resolution.primary_entity else "",
        )
        return resolution
