# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip wheel \
        --wheel-dir /build/wheels \
        -r requirements.txt


FROM python:${PYTHON_VERSION}-slim AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/workspace/app \
    SOORIN_HOST=0.0.0.0 \
    SOORIN_PORT=6998

WORKDIR /workspace

RUN groupadd --gid "${APP_GID}" soorin \
    && useradd \
        --uid "${APP_UID}" \
        --gid "${APP_GID}" \
        --create-home \
        --shell /usr/sbin/nologin \
        soorin

COPY --from=builder /build/wheels /tmp/wheels
COPY requirements.txt /tmp/requirements.txt

RUN python -m pip install \
        --no-index \
        --find-links=/tmp/wheels \
        -r /tmp/requirements.txt \
    && rm -rf /tmp/wheels /tmp/requirements.txt

COPY --chown=soorin:soorin app ./app
COPY --chown=soorin:soorin data ./data
COPY --chown=soorin:soorin lib ./lib
COPY --chown=soorin:soorin script ./script

USER soorin

EXPOSE 6998 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6998/health', timeout=3)" || exit 1

CMD [
    "python",
    "-m",
    "uvicorn",
    "src.api.main:app",
    "--host",
    "0.0.0.0",
    "--port",
    "6998"
]