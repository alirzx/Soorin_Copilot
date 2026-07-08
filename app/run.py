"""Run the Soorin Copilot API locally."""

from __future__ import annotations

import uvicorn

from src.config.settings import get_settings


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "src.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.api_reload,
        log_level=settings.log_level.lower(),
    )
