"""Run Soorin Copilot locally."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys

import uvicorn

from src.config.settings import get_settings
from src.core.observability.logging import configure_application_logging


def configure_logging() -> None:
    configure_application_logging(get_settings())


def run_api() -> None:
    settings = get_settings()
    logging.getLogger(__name__).info(
        "event=api_start host=%s port=%s reload=%s",
        settings.api_host,
        settings.api_port,
        settings.api_reload,
    )
    uvicorn.run(
        "src.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.api_reload,
        log_level=settings.log_level.lower(),
    )


def run_web() -> None:
    settings = get_settings()
    logging.getLogger(__name__).info(
        "event=web_start port=%s api_base_url=%s",
        settings.streamlit_server_port,
        settings.api_base_url,
    )

    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "app/app_st.py",
        "--server.port",
        str(settings.streamlit_server_port),
    ]
    subprocess.run(cmd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Soorin Copilot services.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--api", action="store_true", help="Run FastAPI backend.")
    group.add_argument("--web", action="store_true", help="Run Streamlit UI.")

    args = parser.parse_args()
    configure_logging()

    if args.api:
        run_api()
    elif args.web:
        run_web()


if __name__ == "__main__":
    main()
