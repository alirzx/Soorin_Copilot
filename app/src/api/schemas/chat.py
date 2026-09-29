"""Stable public response contracts for Copilot health and chat endpoints."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: Literal["ok"]


class LLMDeploymentHealth(BaseModel):
    """Shared deployment fields while preserving provider-specific diagnostics."""

    model_config = ConfigDict(extra="allow")

    enabled: bool
    ready: bool
    deployment: str
    provider: str | None = None
    model: str | None = None
    host: str | None = None
    missing: list[str] = Field(default_factory=list)


class LLMHealthData(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool
    ready: bool
    provider: str
    model: str
    deployment: str
    reason: str | None = None
    router: LLMDeploymentHealth | None = None
    planner: LLMDeploymentHealth | None = None
    investigator: LLMDeploymentHealth | None = None
    chat: LLMDeploymentHealth | None = None


class LLMHealthResponse(BaseModel):
    status: Literal["ok"]
    data: LLMHealthData
    warnings: list[str]
    errors: list[dict[str, Any]]


class ChatResponseData(BaseModel):
    session_id: str
    answer: str
    provider: str
    model: str


class ChatError(BaseModel):
    reason: str
    message: str


class ChatResponse(BaseModel):
    status: Literal["ok", "error"]
    data: ChatResponseData | None = None
    warnings: list[str]
    errors: list[ChatError]
