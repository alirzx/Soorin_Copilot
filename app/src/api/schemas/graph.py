"""Typed graph API schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GraphDegree(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    in_: int = Field(alias="in", ge=0)
    out: int = Field(ge=0)
    total: int = Field(ge=0)


class GraphStatusResponse(BaseModel):
    loaded: bool
    nodes: int = Field(ge=0)
    edges: int = Field(ge=0)
    directed: bool
    artifact_available: bool
    active_graph_loaded_at: str | None = None
    active_graph_source: str | None = None
    active_graph_version: str | None = None
    refresh_enabled: bool = False
    refresh_running: bool = False
    refresh_interval_seconds: int = Field(default=0, ge=0)
    refresh_last_attempt_at: str | None = None
    refresh_last_success_at: str | None = None
    refresh_last_failure_at: str | None = None
    refresh_last_error_type: str | None = None
    refresh_last_error_message: str | None = None
    refresh_consecutive_failures: int = Field(default=0, ge=0)
    raw_snapshot_path: str | None = None
    processed_snapshot_path: str | None = None
    last_known_good: bool = False


class GraphStatsResponse(BaseModel):
    total_nodes: int = Field(ge=0)
    total_edges: int = Field(ge=0)
    avg_degree: float = Field(ge=0)
    top_destinations: list[dict[str, int | str]]
    top_sources: list[dict[str, int | str]]
    ip_range_distribution: dict[str, int]


class GraphNodeResponse(BaseModel):
    ip: str
    found: bool
    degree: GraphDegree


class GraphNeighborRecord(BaseModel):
    ip: str
    direction: Literal["in", "out"]
    edge_weight: int = Field(
        ge=1,
        description="Duplicate validated topology records for this observed communication edge.",
    )


class GraphNeighborsResponse(BaseModel):
    target_ip: str
    found: bool
    direction: Literal["in", "out", "both"]
    total: int = Field(ge=0)
    returned: int = Field(ge=0)
    neighbors: list[GraphNeighborRecord]


class GraphContextResponse(BaseModel):
    target_ip: str
    node_found: bool
    degree: GraphDegree
    top_inbound_peers: list[str]
    top_outbound_peers: list[str]
    bidirectional_peers: list[str]
    subnets_reached: list[str]
    limitations: list[str]


class GraphPathResponse(BaseModel):
    source: str
    target: str
    found: bool
    path: list[str]
    edge_count: int = Field(ge=0)
    semantics: str = Field(
        description="Observed communication-graph path, not proof of routed network path."
    )
    reason: str | None = None
