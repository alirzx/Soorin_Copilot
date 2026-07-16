"""FastAPI application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from src.api.dependencies import get_graph_refresh_service as get_api_graph_refresh_service
from src.api.graph_routes import router as graph_router
from src.api.routes import router
from src.config.settings import get_settings
from src.core.graph.refresh import set_graph_refresh_service


logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="Soorin Copilot API")
    app.include_router(router)
    app.include_router(graph_router)

    @app.on_event("startup")
    def on_startup() -> None:
        settings = get_settings()
        settings.validate_selected_llm_deployments()
        router_deployment = settings.deployment_for_purpose("intent_router")
        chat_deployment = settings.deployment_for_purpose("chat")
        logger.info("==================== API STARTUP ====================")
        logger.info(
            "event=application_startup host=%s port=%s provider=%s router_deployment=%s router_model=%s chat_deployment=%s chat_model=%s",
            settings.api_host,
            settings.api_port,
            settings.llm_provider,
            router_deployment.name,
            router_deployment.model,
            chat_deployment.name,
            chat_deployment.model,
        )
        refresh_service = get_api_graph_refresh_service()
        set_graph_refresh_service(refresh_service)
        loaded = refresh_service.load_last_known_good()
        logger.info(
            "event=graph_startup_last_known_good loaded=%s refresh_enabled=%s refresh_on_startup=%s interval_seconds=%s",
            loaded,
            settings.graph_auto_refresh_enabled,
            settings.graph_refresh_on_startup,
            settings.graph_refresh_interval_seconds,
        )
        refresh_service.start_background()

    @app.on_event("shutdown")
    def on_shutdown() -> None:
        refresh_service = get_api_graph_refresh_service()
        refresh_service.stop_background()
        set_graph_refresh_service(None)
        logger.info("event=application_shutdown")

    return app


app = create_app()
