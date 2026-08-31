"""FastAPI application factory."""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from src.api.auth import verify_api_key
from src.api.dependencies import get_graph_refresh_service as get_api_graph_refresh_service
from src.api.graph_routes import router as graph_router
from src.api.local_simulation_routes import router as local_simulation_router
from src.api.routes import copilot_service, router
from src.config.settings import get_settings
from src.core.graph.refresh import set_graph_refresh_service
from src.core.observability.metrics import PrometheusASGIMiddleware, configure_metrics


logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    metrics = configure_metrics(settings.metrics_enabled)
    app = FastAPI(title="Soorin Copilot API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(PrometheusASGIMiddleware)
    app.include_router(router)
    app.include_router(graph_router)
    app.include_router(local_simulation_router)

    if settings.metrics_enabled:
        def prometheus_metrics(_auth: None = Depends(verify_api_key)) -> Response:
            return Response(
                content=metrics.render(),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

        app.add_api_route(
            settings.metrics_path,
            prometheus_metrics,
            methods=["GET"],
            tags=["internal"],
            summary="Prometheus metrics",
        )

    @app.on_event("startup")
    def on_startup() -> None:
        settings.validate_selected_llm_deployments()
        router_deployment = settings.deployment_for_purpose("intent_router")
        chat_deployment = settings.deployment_for_purpose("chat")
        logger.info("==================== API STARTUP ====================")
        logger.info(
            "event=application_startup host=%s port=%s provider=%s router_deployment=%s router_model=%s planner_enabled=%s planner_deployment=%s planner_model=%s chat_deployment=%s chat_model=%s agent_max_calls=%s agent_max_graph_depth=%s agent_max_concurrency=%s",
            settings.api_host,
            settings.api_port,
            settings.llm_provider,
            router_deployment.name,
            router_deployment.model,
            settings.planner_enabled,
            "planner",
            settings.deployment_for_purpose("planner").model,
            chat_deployment.name,
            chat_deployment.model,
            settings.agent_max_capability_calls,
            settings.agent_max_graph_depth,
            settings.agent_executor_max_concurrency,
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
        copilot_service.close()
        logger.info("event=application_shutdown")

    return app


app = create_app()
