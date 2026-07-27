ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /build

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv

COPY requirements-torch-cpu.txt requirements.txt ./

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -r requirements-torch-cpu.txt \
    && python -m pip install -r requirements.txt \
    && python -m pip check \
    && python -c 'import torch; assert torch.version.cuda is None; assert "+cpu" in torch.__version__' \
    && python -c 'import importlib.metadata as m; bad=[d.metadata["Name"] for d in m.distributions() if (d.metadata.get("Name") or "").lower().startswith(("nvidia-", "cuda-"))]; assert not bad, bad'


FROM python:${PYTHON_VERSION}-slim AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace/app \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    SOORIN_HOST=0.0.0.0 \
    SOORIN_PORT=6998 \
    HF_HOME=/home/soorin/.cache/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false

WORKDIR /workspace

RUN groupadd --gid "${APP_GID}" soorin \
    && useradd \
        --uid "${APP_UID}" \
        --gid "${APP_GID}" \
        --create-home \
        --shell /usr/sbin/nologin \
        soorin \
    && apt-get update \
    && apt-get install --no-install-recommends -y libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY --chown=soorin:soorin app ./app
COPY --chown=soorin:soorin lib ./lib

RUN mkdir -p \
        /workspace/data/raw \
        /workspace/data/processed \
        /workspace/data/runtime/logs \
        /workspace/data/runtime/evidence \
        /workspace/data/qdrant-local \
        /home/soorin/.cache/huggingface \
    && chown -R soorin:soorin \
        /workspace/data \
        /home/soorin

USER soorin

EXPOSE 6998 8501

CMD ["python", "app/run.py", "--api"]
