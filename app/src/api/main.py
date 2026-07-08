"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from src.api.routes import router


def create_app() -> FastAPI:
    app = FastAPI(title="Soorin Copilot API")
    app.include_router(router)
    return app


app = create_app()
