# Soorin Copilot Deployment

This document is the canonical deployment reference for the current Docker/Compose runtime.

## Configuration Model

The repository-root `.env` is the private runtime configuration consumed by the application, Makefile, and Compose deployment. `.env.example` is the tracked, secret-free schema.

```bash
cp .env.example .env
```

Keep `.env` private. Do not commit Product credentials, LLM API keys, Copilot API keys, Neo4j passwords, Grafana credentials, or other secrets.

The current deployment uses the repository-root `.env`; legacy `app/.env` or `compose.env` files are not part of the canonical deployment layout.

## Services

The Compose stack contains the following principal services.

### API

- container: `soorin-copilot-api`
- internal port: `6998`
- host port: `SOORIN_API_HOST_PORT`
- host bind address: `SOORIN_API_BIND_IP`
- command: `python app/run.py --api`
- non-root runtime user: `soorin`
- persistent data target: `/workspace/data`
- Hugging Face cache target: `/home/soorin/.cache/huggingface`

### UI

- container: `soorin-copilot-ui`
- internal Streamlit port: `8501`
- host port: `SOORIN_UI_HOST_PORT`
- host bind address: `SOORIN_UI_BIND_IP`
- API service URL inside Compose: `http://api:6998`

### Neo4j

- Neo4j Community 2026.07.1
- Bolt port inside the network: `7687`
- host Bolt binding controlled by `SOORIN_NEO4J_BOLT_BIND_IP` and `SOORIN_NEO4J_BOLT_HOST_PORT`
- persistent named volumes for database and logs

The API uses `bolt://neo4j:7687` inside the Compose network.

### Observability profile

The optional `observability` profile includes:

- Prometheus — host port `SOORIN_OBSERVABILITY_PROMETHEUS_PORT`
- Loki — host port `SOORIN_OBSERVABILITY_LOKI_PORT`
- Alloy — Docker log collection
- Grafana — host port `SOORIN_OBSERVABILITY_GRAFANA_PORT`

The default observability host bindings are loopback-only. Prometheus scrapes the authenticated Copilot metrics endpoint on `api:6998`.

## Docker Image

The Dockerfile uses a multi-stage Python 3.12 slim build.

Build properties:

- a dedicated virtual environment is assembled in the builder stage;
- CPU-only Torch is installed from the PyTorch CPU index;
- `pip check` is run during build;
- the build verifies that CUDA/NVIDIA Python packages are not present;
- runtime contains `app/` and `lib/` but no private `.env`, Qdrant data, Hugging Face cache, or runtime data;
- the runtime process executes as the non-root `soorin` user.

The image exposes API port `6998` and Streamlit port `8501`.

## Persistent Data and Mounts

### Application data

The repository/runtime `data/` directory is mounted read/write at:

```text
/workspace/data
```

It contains runtime material such as:

```text
data/
├── raw/
├── processed/
├── qdrant-local/
└── runtime/
    ├── logs/
    └── evidence/
```

Normal deployment must preserve this tree across API container recreation.

### Hugging Face cache

The host model cache is configured through:

```env
SOORIN_HF_CACHE_HOST_PATH=/absolute/host/path
```

Compose mounts it read-only at:

```text
/home/soorin/.cache/huggingface
```

The Makefile resolves the same variable for preflight and image-preflight checks. Absolute paths are used directly; relative paths resolve from the repository root. If the variable is absent, the Makefile falls back to `./huggingface`.

For a server installation, use a stable absolute path, for example:

```env
SOORIN_HF_CACHE_HOST_PATH=/srv/soorin-copilot/huggingface
```

The configured embedding snapshot must already exist when offline/local-files-only runtime is enabled.

### Qdrant

The normal RAG runtime uses local Qdrant storage under:

```text
/workspace/data/qdrant-local
```

The collection name, embedding dimension, embedding model, and exact model revision are configured through `.env` and validated by `make preflight`.

### Neo4j

Neo4j database/log state lives in Docker named volumes. API/UI recreation must not remove these volumes.

## Makefile Deployment Contract

### Show resolved configuration

```bash
make show-config
```

This prints non-secret deployment values including image tag, data directory, resolved Hugging Face cache, and API/UI bindings.

### Validate Compose syntax

```bash
make config
```

### Preflight runtime prerequisites

```bash
make preflight
```

The preflight validates:

- `docker-compose.yaml`, `.env`, and Dockerfile exist;
- Docker and Compose v2 are available;
- a usable Python interpreter exists;
- the data directory exists;
- the resolved Hugging Face cache exists;
- configured RAG collection/model/revision are present;
- Qdrant metadata contains the configured collection;
- the configured model snapshot contains `config.json`;
- embedding hidden size matches the configured dimension;
- Compose interpolation/configuration is valid.

### Build current source

```bash
make build
```

or, when a fully clean dependency rebuild is required:

```bash
make build-no-cache
```

The image tag comes from `SOORIN_IMAGE_TAG`.

### Validate the built/loaded image

```bash
make preflight-image
```

This also verifies that the image user can write `/workspace/data` and read the mounted embedding snapshot.

Additional image checks:

```bash
make inspect-image
make inspect-size
```

`inspect-image` verifies the CPU-only Torch contract and checks that runtime data, secrets, and model-cache files were not baked into the image.

### Deploy current source

```bash
make deploy
```

This performs preflight, builds the image, validates it, and recreates API/UI with the current image while preserving persistent service state.

### Start a prebuilt image

```bash
make up
```

### Recreate without rebuilding

```bash
make restart
```

### Health checks

```bash
make health
```

The health target checks:

- API `/health`
- API `/openapi.json`
- Streamlit `/_stcore/health`

## Server Environment

The server `.env` should be derived from the same `.env.example` version as the deployed code, but values must reflect the server environment rather than a developer machine.

Keep these categories synchronized with the validated local configuration where behavior must remain identical:

- LLM provider/role settings and token budgets;
- Router/Planner behavior;
- Product endpoint paths and memory backends;
- Neo4j query/sync settings;
- graph enrichment policy;
- structured context limits;
- RAG collection/model/revision/dimension;
- conversation and memory budgets;
- observability behavior.

Keep these values server-specific:

- secrets and credentials;
- external Product URLs where topology differs;
- host bind addresses and ports;
- image release tag;
- `SOORIN_HF_CACHE_HOST_PATH`;
- any host filesystem paths.

Do not replace an established server `.env` wholesale with `.env.example`; compare the key set and deliberately preserve server secrets and machine-specific values.

## Recommended Server Rollout

For a source-based server deployment:

```bash
git pull --ff-only <remote> main
make show-config
make config
make preflight
make build
make preflight-image
make inspect-image
make deploy
make health
```

After startup, verify container state and service logs:

```bash
make ps
make logs
```

Also verify protected API and LLM health using the configured Copilot authentication.

## Network Exposure

Use `127.0.0.1` host bindings for services that do not need remote access. Use `0.0.0.0` or a specific server interface only when upstream network controls intentionally expose the service.

Neo4j, Prometheus, Loki, and Grafana should remain private unless there is an explicit operational requirement for remote access.

## Data Safety

`make down` removes containers/network but does not intentionally remove persistent host data or named volumes. Do not use destructive Docker volume removal during routine deployment.

Before moving or replacing a local Qdrant data tree, stop processes that hold its files and copy it as a consistent snapshot.