"""FastAPI application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from src.api.routes import router
from src.config.settings import get_settings


logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="Soorin Copilot API")
    app.include_router(router)

    @app.on_event("startup")
    def on_startup() -> None:
        settings = get_settings()
        logger.info(
            "event=application_startup host=%s port=%s provider=%s model=%s",
            settings.api_host,
            settings.api_port,
            settings.llm_provider,
            settings.arvan_model,
        )

    return app


app = create_app()
