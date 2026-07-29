"""Bearer API key authentication for Copilot API."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Header, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config.settings import get_settings

bearer_scheme = HTTPBearer(auto_error=False)


def verify_api_key(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    copilot_api_key_header: Annotated[str | None, Header(alias="Soorin_copilot_api_key")] = None,
) -> None:
    settings = get_settings()
    if not settings.copilot_api_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid Bearer token",
        )

    if copilot_api_key_header is not None:
        candidate = copilot_api_key_header.strip()
    elif credentials is not None:
        candidate = credentials.credentials
    else:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header",
        )

    if not hmac.compare_digest(candidate, settings.copilot_api_key):
        raise HTTPException(
            status_code=401,
            detail="Invalid Bearer token",
        )
