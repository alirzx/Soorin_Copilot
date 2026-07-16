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
CIDR_CANDIDATE_RE = re.compile(r"(?<![0-9A-Fa-f:.])(?:[0-9A-Fa-f:.]+)/\d{1,3}(?!\d)")
REFERENTIAL_ENTITY_PATTERNS: dict[str, re.Pattern[str]] = {
    "this_entity": re.compile(r"\b(?:this|that|the\s+selected|the\s+current)\s+(?:ip|asset|host|node)\b", re.IGNORECASE),
    "same_entity": re.compile(r"\b(?:the\s+)?(?:same|previous)\s+(?:ip|asset|host|node)\b", re.IGNORECASE),
    "what_is_this": re.compile(r"\bwhat\s+is\s+this\b", re.IGNORECASE),
    "about_this": re.compile(
        r"\b(?:tell|explain|analy[sz]e|investigate|show)\b.{0,40}\b(?:this|this\s+one|that|that\s+one)\b",
        re.IGNORECASE,
    ),
    "what_know_it": re.compile(
        r"\bwhat\s+(?:do\s+we|can\s+you|do\s+you|can\s+we)\s+know\s+about\s+(?:it|this|this\s+one|that\s+one)\b",
        re.IGNORECASE,
    ),
    "available_info_reference": re.compile(
        r"\b(?:do\s+you\s+have|is\s+there)\s+(?:any\s+)?(?:info|information|anything\s+useful)\b.{0,50}\b(?:it|this|this\s+one|that\s+one|asset|ip|host|node)\b",
        re.IGNORECASE,
    ),
    "details_about_it": re.compile(
        r"\b(?:more\s+details|tell\s+me\s+more|what\s+else|what\s+can\s+you\s+tell\s+me)\b.{0,50}\b(?:it|this|this\s+one|that\s+one)?\b",
        re.IGNORECASE,
    ),
    "deeper_followup": re.compile(
        r"\b(?:go\s+deeper|continue|continue\s+with\s+(?:this|the\s+same\s+asset)|now\s+show|analy[sz]e\s+further|expand\s+the\s+analysis)\b",
        re.IGNORECASE,
    ),
    "bounded_it_reference": re.compile(
        r"\b(?:of|from|for|about)\s+it\b|\b(?:evidence|connections?|behavio[u]?r|information|data)\s+(?:we\s+have\s+)?(?:for|from|about)\s+it\b",
        re.IGNORECASE,
    ),
    "possessive_evidence": re.compile(
        r"\b(?:all\s+of\s+)?its\s+(?:evidence|connections?|neighbors?|behavio[u]?r|data|details?|information|profile|topology)\b",
        re.IGNORECASE,
    ),
    "its_graph_attribute": re.compile(
        r"\bits\s+(?:connections?|communications?|inbound|outbound|peers?|neighbors?|relationships?|graph|topology)\b",
        re.IGNORECASE,
    ),
    "its_asset_attribute": re.compile(
        r"\bits\s+(?:data|details?|info|information|behavior|behaviour|role|profile|summary)\b",
        re.IGNORECASE,
    ),
    "it_graph_attribute": re.compile(
        r"\bit\s+(?:has|have|show|include|communicates?|connects?|connected|reaches?|sends?|receives?)\b"
        r".*\b(?:connections?|communications?|inbound|outbound|peers?|neighbors?|relationships?)\b",
        re.IGNORECASE,
    ),
    "compare_with_reference": re.compile(
        r"\b(?:compare|contrast|relate)\b.{0,60}\b(?:it|this|that|this\s+asset|that\s+asset)\b",
        re.IGNORECASE,
    ),
}
PAIR_REFERENCE_PATTERNS: dict[str, re.Pattern[str]] = {
    "between_them": re.compile(r"\bbetween\s+(?:them|these\s+two|those\s+two|both)\b", re.IGNORECASE),
    "them_action": re.compile(
        r"\b(?:compare|contrast|relate|show|explain|analy[sz]e|investigate|check|find|trace)\b.{0,80}\bthem\b"
        r"|\bthem\b.{0,80}\b(?:relationship|connection|connected|path|shared|common|peers|neighbors|outbound|inbound|reach|position)",
        re.IGNORECASE,
    ),
    "both_reference": re.compile(
        r"\b(?:compare|contrast|relate|show|explain|analy[sz]e|investigate|check)\b.{0,80}\bboth\b"
        r"|\bboth\s+(?:assets|ips|hosts|nodes)\b",
        re.IGNORECASE,
    ),
    "both_assets_subject": re.compile(r"\b(?:what\s+do\s+)?both\s+assets\s+(?:share|have|show)\b", re.IGNORECASE),
    "two_asset_reference": re.compile(
        r"\b(?:these\s+two|those\s+two|the\s+two)(?:\s+(?:assets|ips|hosts|nodes))?\b",
        re.IGNORECASE,
    ),
    "those_assets": re.compile(r"\bthose\s+(?:assets|ips|hosts|nodes)\b", re.IGNORECASE),
    "their_relationship": re.compile(r"\btheir\s+(?:relationship|connection|connections|path|neighbors|peers|topology|positions?)\b", re.IGNORECASE),
    "shared_peers": re.compile(r"\b(?:shared|common)\s+(?:peers|neighbors|connections|subnets)\b", re.IGNORECASE),
    "which_one": re.compile(r"\bwhich\s+one\b", re.IGNORECASE),
}
REFERENCE_SUPPRESSION_PATTERNS: dict[str, re.Pattern[str]] = {
    "not_about_asset": re.compile(r"\bnot\s+about\s+(?:this|the|that)?\s*(?:asset|ip|host|node)\b", re.IGNORECASE),
    "not_selected_node": re.compile(r"\bnot\s+about\s+(?:the\s+)?selected\s+(?:node|asset|ip|host)\b", re.IGNORECASE),
    "in_general": re.compile(r"\b(?:in\s+general|generally\s+speaking)\b", re.IGNORECASE),
    "concept_only": re.compile(r"\bI\s+mean\s+the\s+concept\b", re.IGNORECASE),
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


def _extract_subnets(message: str) -> tuple[tuple[str, ...], str]:
    """Return canonical CIDRs and text with valid CIDRs masked from host extraction."""
    networks: list[str] = []
    spans: list[tuple[int, int]] = []
    for match in CIDR_CANDIDATE_RE.finditer(message or ""):
        try:
            network = ipaddress.ip_network(match.group(0), strict=False)
        except ValueError:
            continue
        canonical = str(network)
        if canonical not in networks:
            networks.append(canonical)
        spans.append(match.span())
    masked = list(message or "")
    for start, end in spans:
        masked[start:end] = " " * (end - start)
    return tuple(networks), "".join(masked)


class EntityResolver:
    """Resolve only IPv4 entities for the frozen baseline."""

    def _detect_reference(self, message: str) -> tuple[bool, str | None]:
        for name, pattern in REFERENTIAL_ENTITY_PATTERNS.items():
            if pattern.search(message or ""):
                return True, name
        return False, None

    def _detect_pair_reference(self, message: str) -> tuple[bool, str | None]:
        for name, pattern in PAIR_REFERENCE_PATTERNS.items():
            if pattern.search(message or ""):
                return True, name
        return False, None

    def _detect_reference_suppression(self, message: str) -> tuple[bool, str | None]:
        for name, pattern in REFERENCE_SUPPRESSION_PATTERNS.items():
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
            "event=entity_resolution_started request_id=%s message_preview=%r ui_context_present=%s active_ip_present=%s active_entity_count=%s",
            request_id,
            compact_preview(message),
            bool(ui_context),
            bool(routing_state and routing_state.active_ip),
            len(routing_state.active_entities) if routing_state else 0,
        )

        subnet_constraints, host_text = _extract_subnets(message)
        explicit_candidates = IPV4_CANDIDATE_RE.findall(host_text)
        message_ips: list[str] = []
        seen: set[str] = set()
        for candidate in explicit_candidates:
            ip = _valid_ipv4(candidate)
            if ip and ip not in seen:
                message_ips.append(ip)
                seen.add(ip)

        ui_selected_ip = _valid_ipv4(str((ui_context or {}).get("selected_ip") or ""))
        active_ip = _valid_ipv4(routing_state.active_ip if routing_state else None)
        active_entities = [
            ip for raw in (routing_state.active_entities if routing_state else ())
            if (ip := _valid_ipv4(raw))
        ]
        reference_detected, reference_type = self._detect_reference(message)
        pair_reference_detected, pair_reference_type = self._detect_pair_reference(message)
        reference_suppressed, suppression_reason = self._detect_reference_suppression(message)
        if reference_suppressed:
            reference_detected = False
            reference_type = None
            pair_reference_detected = False
            pair_reference_type = None

        if (
            len(message_ips) == 1
            and reference_detected
            and reference_type == "compare_with_reference"
            and active_ip
            and active_ip != message_ips[0]
            and not reference_suppressed
        ):
            entities = [
                ResolvedEntity(type="ip", value=active_ip, source="conversation"),
                ResolvedEntity(type="ip", value=message_ips[0], source="message"),
            ]
            resolution = EntityResolution(
                status="resolved",
                entities=entities,
                primary_entity=None,
                entity_mode="multiple",
                candidate_count=len(entities),
                explicit_candidate_count=len(explicit_candidates),
                valid_entity_count=len(entities),
                reference_detected=True,
                reference_type=reference_type,
                reference_suppressed=reference_suppressed,
                suppression_reason=suppression_reason,
            )
        elif len(message_ips) > 1:
            entities = [ResolvedEntity(type="ip", value=ip, source="message") for ip in message_ips]
            resolution = EntityResolution(
                status="resolved",
                entities=entities,
                primary_entity=entities[0] if len(entities) == 1 else None,
                entity_mode="multiple",
                candidate_count=len(message_ips),
                explicit_candidate_count=len(explicit_candidates),
                valid_entity_count=len(message_ips),
                reference_detected=reference_detected,
                reference_type=reference_type,
                reference_suppressed=reference_suppressed,
                suppression_reason=suppression_reason,
            )
        elif len(message_ips) == 1:
            primary = ResolvedEntity(type="ip", value=message_ips[0], source="message")
            resolution = EntityResolution(
                status="resolved",
                entities=[primary],
                primary_entity=primary,
                entity_mode="single",
                candidate_count=1,
                explicit_candidate_count=len(explicit_candidates),
                valid_entity_count=1,
                reference_detected=reference_detected,
                reference_type=reference_type,
                reference_suppressed=reference_suppressed,
                suppression_reason=suppression_reason,
            )
        elif ui_selected_ip and not reference_suppressed:
            primary = ResolvedEntity(type="ip", value=ui_selected_ip, source="ui")
            resolution = EntityResolution(
                status="resolved",
                entities=[primary],
                primary_entity=primary,
                entity_mode="single",
                candidate_count=1,
                explicit_candidate_count=len(explicit_candidates),
                valid_entity_count=1,
                reference_detected=reference_detected,
                reference_type=reference_type,
                reference_suppressed=reference_suppressed,
                suppression_reason=suppression_reason,
            )
        elif pair_reference_detected and len(active_entities) >= 2 and not reference_suppressed:
            entities = [
                ResolvedEntity(type="ip", value=ip, source="conversation")
                for ip in active_entities[:2]
            ]
            resolution = EntityResolution(
                status="resolved",
                entities=entities,
                primary_entity=None,
                entity_mode="multiple",
                candidate_count=len(entities),
                explicit_candidate_count=len(explicit_candidates),
                valid_entity_count=len(entities),
                reference_detected=True,
                reference_type=pair_reference_type,
                reference_suppressed=reference_suppressed,
                suppression_reason=suppression_reason,
            )
        elif reference_detected and active_ip and not reference_suppressed:
            primary = ResolvedEntity(type="ip", value=active_ip, source="conversation")
            resolution = EntityResolution(
                status="resolved",
                entities=[primary],
                primary_entity=primary,
                entity_mode="single",
                candidate_count=1,
                explicit_candidate_count=len(explicit_candidates),
                valid_entity_count=1,
                reference_detected=True,
                reference_type=reference_type,
                reference_suppressed=reference_suppressed,
                suppression_reason=suppression_reason,
            )
        else:
            status = "invalid" if explicit_candidates or subnet_constraints else "none"
            mode = "invalid" if explicit_candidates or subnet_constraints else "none"
            resolution = EntityResolution(
                status=status,
                entities=[],
                primary_entity=None,
                entity_mode=mode,
                candidate_count=0,
                explicit_candidate_count=len(explicit_candidates),
                valid_entity_count=0,
                reference_detected=reference_detected or pair_reference_detected,
                reference_type=reference_type or pair_reference_type,
                reference_suppressed=reference_suppressed,
                suppression_reason=suppression_reason,
            )

        if subnet_constraints:
            resolution = EntityResolution(
                **{
                    **resolution.__dict__,
                    "subnet_constraints": subnet_constraints,
                    "unsupported_constraints": subnet_constraints,
                }
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "event=entity_resolution_completed request_id=%s status=%s entity_mode=%s explicit_candidate_count=%s valid_entity_count=%s subnet_constraint_count=%s unsupported_constraint_count=%s reference_detected=%s reference_type=%s reference_suppressed=%s suppression_reason=%s ui_selected_ip_present=%s active_ip_present=%s active_entity_count=%s primary_entity_type=%s primary_entity_value=%s primary_entity_source=%s latency_ms=%s",
            request_id,
            resolution.status,
            resolution.entity_mode,
            resolution.explicit_candidate_count,
            resolution.valid_entity_count,
            len(resolution.subnet_constraints),
            len(resolution.unsupported_constraints),
            resolution.reference_detected,
            resolution.reference_type or "",
            resolution.reference_suppressed,
            resolution.suppression_reason or "",
            bool(ui_selected_ip),
            bool(active_ip),
            len(active_entities),
            resolution.primary_entity.type if resolution.primary_entity else "",
            resolution.primary_entity.value if resolution.primary_entity else "",
            resolution.primary_entity.source if resolution.primary_entity else "",
            latency_ms,
        )
        return resolution
