"""Typed graph API routes."""

from __future__ import annotations

import ipaddress
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.auth import verify_api_key
from src.api.dependencies import get_graph_service
from src.api.schemas.graph import (
    GraphContextResponse,
    GraphNeighborsResponse,
    GraphNodeResponse,
    GraphPathResponse,
    GraphStatsResponse,
    GraphStatusResponse,
    GraphTopologyResponse,
)
from src.config.settings import get_settings
from src.core.context.models import ResolvedEntity
from src.core.graph.retrieval import GraphRetrievalSpec
from src.core.graph.service import GraphService


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/graph", tags=["graph"])


def _validate_ip(value: str, *, field_name: str = "ip") -> str:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid {field_name}.") from exc


@router.get("/status", response_model=GraphStatusResponse)
def graph_status(
    service: GraphService = Depends(get_graph_service),
    _auth: None = Depends(verify_api_key),
) -> GraphStatusResponse:
    status = service.status()
    logger.info(
        "event=graph_api_status loaded=%s nodes=%s edges=%s artifact_available=%s refresh_enabled=%s refresh_running=%s refresh_consecutive_failures=%s",
        status.loaded,
        status.nodes,
        status.edges,
        status.artifact_available,
        status.refresh_enabled,
        status.refresh_running,
        status.refresh_consecutive_failures,
    )
    return GraphStatusResponse(**status.__dict__)


@router.get("/stats", response_model=GraphStatsResponse)
def graph_stats(
    service: GraphService = Depends(get_graph_service),
    _auth: None = Depends(verify_api_key),
) -> GraphStatsResponse:
    stats = service.stats()
    logger.info(
        "event=graph_api_stats nodes=%s edges=%s",
        stats.get("total_nodes"),
        stats.get("total_edges"),
    )
    return GraphStatsResponse(**stats)


@router.get("/nodes/{ip}", response_model=GraphNodeResponse)
def graph_node(
    ip: str,
    service: GraphService = Depends(get_graph_service),
    _auth: None = Depends(verify_api_key),
) -> GraphNodeResponse:
    target_ip = _validate_ip(ip)
    result = service.node(target_ip)
    logger.info("event=graph_api_node target_ip=%s found=%s", target_ip, result["found"])
    return GraphNodeResponse(**result)


@router.get("/nodes/{ip}/neighbors", response_model=GraphNeighborsResponse)
def graph_neighbors(
    ip: str,
    direction: Literal["in", "out", "both"] = Query(default="both"),
    limit: int = Query(default=20, gt=0),
    service: GraphService = Depends(get_graph_service),
    _auth: None = Depends(verify_api_key),
) -> GraphNeighborsResponse:
    settings = get_settings()
    if limit > settings.graph_api_max_neighbors:
        raise HTTPException(
            status_code=422,
            detail=f"limit must be <= {settings.graph_api_max_neighbors}.",
        )
    target_ip = _validate_ip(ip)
    result = service.neighbors(target_ip, direction=direction, limit=limit)
    logger.info(
        "event=graph_api_neighbors target_ip=%s direction=%s result_count=%s",
        target_ip,
        direction,
        result["returned"],
    )
    return GraphNeighborsResponse(**result)


@router.get("/nodes/{ip}/context", response_model=GraphContextResponse)
def graph_context(
    ip: str,
    service: GraphService = Depends(get_graph_service),
    _auth: None = Depends(verify_api_key),
) -> GraphContextResponse:
    target_ip = _validate_ip(ip)
    context = service.context(
        GraphRetrievalSpec(
            scope="node_summary",
            direction="both",
            depth=0,
            entities=[ResolvedEntity(type="ip", value=target_ip, source="api")],
        )
    )
    logger.info(
        "event=graph_api_context target_ip=%s found=%s",
        target_ip,
        context["node_found"],
    )
    return GraphContextResponse(
        target_ip=target_ip,
        node_found=bool(context["node_found"]),
        degree={"in": context["in_degree"], "out": context["out_degree"], "total": context["degree"]},
        top_inbound_peers=list(context.get("top_inbound_peers") or []),
        top_outbound_peers=list(context.get("top_outbound_peers") or []),
        bidirectional_peers=list(context.get("bidirectional_peers") or []),
        subnets_reached=list(context.get("subnets_reached") or []),
        limitations=list(context.get("limitations") or []),
    )


@router.get("/path", response_model=GraphPathResponse)
def graph_path(
    source: str = Query(...),
    target: str = Query(...),
    service: GraphService = Depends(get_graph_service),
    _auth: None = Depends(verify_api_key),
) -> GraphPathResponse:
    source_ip = _validate_ip(source, field_name="source")
    target_ip = _validate_ip(target, field_name="target")
    result = service.path(source_ip, target_ip)
    logger.info(
        "event=graph_api_path source_ip=%s target_ip=%s found=%s edge_count=%s",
        source_ip,
        target_ip,
        result["found"],
        result["edge_count"],
    )
    return GraphPathResponse(**result)


@router.get("/topology", response_model=GraphTopologyResponse)
def graph_topology(
    max_nodes: int | None = Query(default=None, gt=0),
    min_degree: int | None = Query(default=None, ge=0),
    subnet: str = Query(default="", max_length=64),
    service: GraphService = Depends(get_graph_service),
    _auth: None = Depends(verify_api_key),
) -> GraphTopologyResponse:
    """Return a bounded active-projection topology for the future UI consumer."""
    result = service.topology(max_nodes=max_nodes, min_degree=min_degree, subnet=subnet)
    logger.info(
        "event=graph_api_topology nodes=%s edges=%s max_nodes=%s min_degree=%s subnet=%s",
        len(result["nodes"]), len(result["edges"]), result["max_nodes"], result["min_degree"], result["subnet"],
    )
    return GraphTopologyResponse(**result)
