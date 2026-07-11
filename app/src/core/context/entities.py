"""Deterministic entity resolution for baseline Copilot context routing."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from typing import Any

from src.core.context.models import EntityResolution, ResolvedEntity, compact_preview
from src.core.memory.routing_state import SessionRoutingState


logger = logging.getLogger(__name__)
IPV4_CANDIDATE_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
REFERENTIAL_ENTITY_PATTERNS: dict[str, re.Pattern[str]] = {
    "this_entity": re.compile(r"\bthis\s+(?:ip|asset|host|node)\b", re.IGNORECASE),
    "the_entity": re.compile(r"\bthe\s+(?:ip|asset|host|node)\b", re.IGNORECASE),
    "same_entity": re.compile(r"\bsame\s+(?:ip|asset|host|node)\b", re.IGNORECASE),
    "its_graph_attribute": re.compile(
        r"\bits\s+(?:connections?|communications?|inbound|outbound|peers?|neighbors?|relationships?|graph|topology)\b",
        re.IGNORECASE,
    ),
    "it_graph_attribute": re.compile(
        r"\bit\s+(?:has|have|show|include|communicates?|connects?|connected|reaches?|sends?|receives?)\b"
        r".*\b(?:connections?|communications?|inbound|outbound|peers?|neighbors?|relationships?)\b",
        re.IGNORECASE,
    ),
}


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

    def _detect_reference(self, message: str) -> tuple[bool, str | None]:
        for name, pattern in REFERENTIAL_ENTITY_PATTERNS.items():
            if pattern.search(message or ""):
                return True, name
        return False, None

    def resolve(
        self,
        message: str,
        ui_context: dict[str, Any] | None = None,
        routing_state: SessionRoutingState | None = None,
        *,
        request_id: str = "",
    ) -> EntityResolution:
        started = time.perf_counter()
        logger.info(
            "event=entity_resolution_started request_id=%s message_preview=%r ui_context_present=%s active_ip_present=%s",
            request_id,
            compact_preview(message),
            bool(ui_context),
            bool(routing_state and routing_state.active_ip),
        )

        explicit_candidates = IPV4_CANDIDATE_RE.findall(message or "")
        message_ips: list[str] = []
        seen: set[str] = set()
        for candidate in explicit_candidates:
            ip = _valid_ipv4(candidate)
            if ip and ip not in seen:
                message_ips.append(ip)
                seen.add(ip)

        ui_selected_ip = _valid_ipv4(str((ui_context or {}).get("selected_ip") or ""))
        active_ip = _valid_ipv4(routing_state.active_ip if routing_state else None)
        reference_detected, reference_type = self._detect_reference(message)

        if len(message_ips) > 1:
            entities = [ResolvedEntity(type="ip", value=ip, source="message") for ip in message_ips]
            resolution = EntityResolution(
                status="ambiguous",
                entities=entities,
                primary_entity=None,
                candidate_count=len(message_ips),
                explicit_candidate_count=len(explicit_candidates),
                valid_entity_count=len(message_ips),
                reference_detected=reference_detected,
                reference_type=reference_type,
            )
        elif len(message_ips) == 1:
            primary = ResolvedEntity(type="ip", value=message_ips[0], source="message")
            resolution = EntityResolution(
                status="resolved",
                entities=[primary],
                primary_entity=primary,
                candidate_count=1,
                explicit_candidate_count=len(explicit_candidates),
                valid_entity_count=1,
                reference_detected=reference_detected,
                reference_type=reference_type,
            )
        elif reference_detected and ui_selected_ip:
            primary = ResolvedEntity(type="ip", value=ui_selected_ip, source="ui")
            resolution = EntityResolution(
                status="resolved",
                entities=[primary],
                primary_entity=primary,
                candidate_count=1,
                explicit_candidate_count=len(explicit_candidates),
                valid_entity_count=1,
                reference_detected=True,
                reference_type=reference_type,
            )
        elif reference_detected and active_ip:
            primary = ResolvedEntity(type="ip", value=active_ip, source="conversation")
            resolution = EntityResolution(
                status="resolved",
                entities=[primary],
                primary_entity=primary,
                candidate_count=1,
                explicit_candidate_count=len(explicit_candidates),
                valid_entity_count=1,
                reference_detected=True,
                reference_type=reference_type,
            )
        else:
            resolution = EntityResolution(
                status="none",
                entities=[],
                primary_entity=None,
                candidate_count=0,
                explicit_candidate_count=len(explicit_candidates),
                valid_entity_count=0,
                reference_detected=reference_detected,
                reference_type=reference_type,
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "event=entity_resolution_completed request_id=%s status=%s explicit_candidate_count=%s valid_entity_count=%s reference_detected=%s reference_type=%s ui_selected_ip_present=%s active_ip_present=%s primary_entity_type=%s primary_entity_value=%s primary_entity_source=%s latency_ms=%s",
            request_id,
            resolution.status,
            resolution.explicit_candidate_count,
            resolution.valid_entity_count,
            resolution.reference_detected,
            resolution.reference_type or "",
            bool(ui_selected_ip),
            bool(active_ip),
            resolution.primary_entity.type if resolution.primary_entity else "",
            resolution.primary_entity.value if resolution.primary_entity else "",
            resolution.primary_entity.source if resolution.primary_entity else "",
            latency_ms,
        )
        return resolution
