"""Bearer API key authentication for Copilot API."""

from __future__ import annotations

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config.settings import get_settings

bearer_scheme = HTTPBearer(auto_error=False)


def verify_api_key(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> None:
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header",
        )
    settings = get_settings()
    if not settings.copilot_api_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid Bearer token",
        )
    if credentials.credentials != settings.copilot_api_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid Bearer token",
        )
