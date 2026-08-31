SHELL := /bin/bash
.DEFAULT_GOAL := help

# ---------------------------------------------------------------------
# Project and deployment inputs
# ---------------------------------------------------------------------

ROOT_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
COMPOSE_FILE ?= $(ROOT_DIR)/docker-compose.yaml
ENV_FILE ?= $(ROOT_DIR)/.env
DOCKERFILE ?= $(ROOT_DIR)/Dockerfile
DATA_DIR ?= $(ROOT_DIR)/data
HF_CACHE_DIR ?= $(ROOT_DIR)/huggingface
EXPORT_DIR ?= $(ROOT_DIR)/dist

COMPOSE := docker compose \
	--project-directory "$(ROOT_DIR)" \
	-f "$(COMPOSE_FILE)" \
	--env-file "$(ENV_FILE)" \
	--profile observability

# Prefer the project virtual environment locally; fall back to system Python.
PYTHON ?= $(shell \
	if [ -x "$(ROOT_DIR)/.venv/bin/python" ]; then \
		printf '%s' "$(ROOT_DIR)/.venv/bin/python"; \
	elif command -v python3 >/dev/null 2>&1; then \
		command -v python3; \
	else \
		printf '%s' "python"; \
	fi \
)

# Read a key from the unified deployment .env without exporting or printing it.
define env_value
$(strip $(shell sed -n 's/^$(1)=//p' "$(ENV_FILE)" 2>/dev/null | tail -n 1))
endef

IMAGE_TAG := $(or $(call env_value,SOORIN_IMAGE_TAG),dev-local)
IMAGE := soorin-copilot:$(IMAGE_TAG)

API_BIND_IP := $(or $(call env_value,SOORIN_API_BIND_IP),127.0.0.1)
API_PORT := $(or $(call env_value,SOORIN_API_HOST_PORT),6998)
UI_BIND_IP := $(or $(call env_value,SOORIN_UI_BIND_IP),127.0.0.1)
UI_PORT := $(or $(call env_value,SOORIN_UI_HOST_PORT),8503)

# Build-time only. These values are baked into the image and intentionally
# do not belong to the deployment .env.
# The defaults preserve the current server image and runtime ownership.
BUILD_APP_UID ?= 1000
BUILD_APP_GID ?= 1000
PYTHON_VERSION ?= 3.12

RAG_COLLECTION := $(call env_value,SOORIN_RAG_COLLECTION)
RAG_MODEL := $(call env_value,SOORIN_RAG_EMBEDDING_MODEL)
RAG_REVISION := $(call env_value,SOORIN_RAG_EMBEDDING_REVISION)
RAG_DIMENSION := $(or $(call env_value,SOORIN_RAG_EMBEDDING_DIMENSION),768)
RAG_MODEL_CACHE_KEY := models--$(subst /,--,$(RAG_MODEL))
RAG_CONFIG_PATH := $(HF_CACHE_DIR)/hub/$(RAG_MODEL_CACHE_KEY)/snapshots/$(RAG_REVISION)/config.json

.PHONY: \
	help show-config preflight check-ports config test-local \
	build build-no-cache up down restart logs ps health \
	inspect-image inspect-size export clean-export

# ---------------------------------------------------------------------
# Help and non-secret configuration
# ---------------------------------------------------------------------

help:
	@echo "Available targets:"
	@echo "  make preflight        Validate files, image, mounts, RAG data and permissions"
	@echo "  make check-ports      Verify configured API and UI host ports are available"
	@echo "  make config           Validate docker-compose.yaml with the unified .env"
	@echo "  make show-config      Print non-secret resolved deployment values"
	@echo "  make test-local       Run the complete offline test suite"
	@echo "  make build            Build the image directly with Docker cache"
	@echo "  make build-no-cache   Build the image directly without Docker cache"
	@echo "  make up               Validate, then start the prebuilt image"
	@echo "  make down             Stop containers while preserving bind-mounted data"
	@echo "  make restart          Recreate containers without rebuilding"
	@echo "  make logs             Follow API and UI logs"
	@echo "  make ps               Show Compose service status"
	@echo "  make health           Check API, OpenAPI and Streamlit health"
	@echo "  make inspect-image    Verify CPU-only and image-content contracts"
	@echo "  make inspect-size     Show the largest installed runtime packages"
	@echo "  make export           Export the image tar and SHA-256 checksum into dist/"
	@echo "  make clean-export     Remove generated image archives from dist/"

show-config:
	@echo "ROOT_DIR=$(ROOT_DIR)"
	@echo "COMPOSE_FILE=$(COMPOSE_FILE)"
	@echo "ENV_FILE=$(ENV_FILE)"
	@echo "IMAGE=$(IMAGE)"
	@echo "DATA_DIR=$(DATA_DIR)"
	@echo "HF_CACHE_DIR=$(HF_CACHE_DIR)"
	@echo "API=$(API_BIND_IP):$(API_PORT)"
	@echo "UI=$(UI_BIND_IP):$(UI_PORT)"
	@echo "BUILD_APP_UID=$(BUILD_APP_UID)"
	@echo "BUILD_APP_GID=$(BUILD_APP_GID)"

# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------

preflight:
	@test -f "$(COMPOSE_FILE)" || \
		(echo "Preflight failed: $(COMPOSE_FILE) is missing."; exit 1)
	@test -f "$(ENV_FILE)" || \
		(echo "Preflight failed: $(ENV_FILE) is missing."; exit 1)
	@test -f "$(DOCKERFILE)" || \
		(echo "Preflight failed: $(DOCKERFILE) is missing."; exit 1)
	@command -v docker >/dev/null 2>&1 || \
		(echo "Preflight failed: docker is unavailable."; exit 1)
	@docker compose version >/dev/null 2>&1 || \
		(echo "Preflight failed: Docker Compose v2 is unavailable."; exit 1)
	@test -x "$(PYTHON)" || command -v "$(PYTHON)" >/dev/null 2>&1 || \
		(echo "Preflight failed: Python interpreter is unavailable: $(PYTHON)"; exit 1)
	@docker image inspect "$(IMAGE)" >/dev/null 2>&1 || \
		(echo "Preflight failed: image is not loaded: $(IMAGE)"; exit 1)

	@test -d "$(DATA_DIR)" || \
		(echo "Preflight failed: data directory does not exist: $(DATA_DIR)"; exit 1)
	@test -d "$(HF_CACHE_DIR)" || \
		(echo "Preflight failed: Hugging Face cache does not exist: $(HF_CACHE_DIR)"; exit 1)

	@test -n "$(RAG_COLLECTION)" || \
		(echo "Preflight failed: SOORIN_RAG_COLLECTION is missing from $(ENV_FILE)."; exit 1)
	@test -n "$(RAG_MODEL)" || \
		(echo "Preflight failed: SOORIN_RAG_EMBEDDING_MODEL is missing from $(ENV_FILE)."; exit 1)
	@test -n "$(RAG_REVISION)" || \
		(echo "Preflight failed: SOORIN_RAG_EMBEDDING_REVISION is missing from $(ENV_FILE)."; exit 1)

	@$(PYTHON) -c 'from pathlib import Path; import sys; \
required=sys.argv[1:]; missing=[p for p in required if not Path(p).is_file()]; \
sys.exit("Preflight failed: missing runtime files: " + ", ".join(missing) if missing else 0)' \
		"$(DATA_DIR)/qdrant-local/meta.json" \
		"$(DATA_DIR)/processed/topology_graph.pkl" \
		"$(DATA_DIR)/raw/topology_raw.json" \
		"$(DATA_DIR)/processed/topology_stats.json" \
		"$(RAG_CONFIG_PATH)"

	@$(PYTHON) -c 'import json, sys; \
meta=json.load(open(sys.argv[1], encoding="utf-8")); collection=sys.argv[2]; \
sys.exit(0 if collection in meta.get("collections", {}) \
else "Preflight failed: configured Qdrant collection is absent from metadata.")' \
		"$(DATA_DIR)/qdrant-local/meta.json" "$(RAG_COLLECTION)"

	@$(PYTHON) -c 'import json, sys; \
config=json.load(open(sys.argv[1], encoding="utf-8")); expected=int(sys.argv[2]); \
actual=int(config.get("hidden_size", 0)); \
sys.exit(0 if actual == expected \
else f"Preflight failed: cached embedding dimension is {actual}, expected {expected}.")' \
		"$(RAG_CONFIG_PATH)" "$(RAG_DIMENSION)"

	@docker run --rm --entrypoint sh \
		-v "$(DATA_DIR):/workspace/data" \
		-v "$(HF_CACHE_DIR):/home/soorin/.cache/huggingface:ro" \
		"$(IMAGE)" -c \
		'test -w /workspace/data && \
		test -r "/home/soorin/.cache/huggingface/hub/$(RAG_MODEL_CACHE_KEY)/snapshots/$(RAG_REVISION)/config.json"' || \
		(echo "Preflight failed: the image user cannot write data or read the model cache."; exit 1)

	@$(COMPOSE) config --quiet
	@echo "Preflight passed."

# Run before starting a stopped stack. Occupied ports are expected while it runs.
check-ports:
	@$(PYTHON) -c 'import socket, sys; \
pairs=((sys.argv[1], int(sys.argv[2]), "API"), (sys.argv[3], int(sys.argv[4]), "UI")); \
failed=[]; \
exec("for host, port, name in pairs:\n" \
"    bind_host = \"0.0.0.0\" if host in {\"::\", \"[::]\"} else host\n" \
"    family = socket.AF_INET6 if \"::\" in bind_host else socket.AF_INET\n" \
"    sock=socket.socket(family)\n" \
"    try:\n" \
"        sock.bind((bind_host, port))\n" \
"    except OSError as exc:\n" \
"        failed.append(f\"{name} {host}:{port} ({exc})\")\n" \
"    finally:\n" \
"        sock.close()\n"); \
sys.exit("Port check failed: " + "; ".join(failed) if failed else 0)' \
		"$(API_BIND_IP)" "$(API_PORT)" "$(UI_BIND_IP)" "$(UI_PORT)"
	@echo "Configured host ports are available."

config:
	@$(COMPOSE) config --quiet
	@echo "Compose configuration is valid."

# ---------------------------------------------------------------------
# Testing and image build
# ---------------------------------------------------------------------

test-local:
	cd "$(ROOT_DIR)" && PYTHONPATH=app "$(PYTHON)" -m pytest -q app/src/tests

build:
	@test -f "$(ENV_FILE)" || \
		(echo "Build failed: $(ENV_FILE) is missing."; exit 1)
	DOCKER_BUILDKIT=1 docker build \
		--build-arg PYTHON_VERSION="$(PYTHON_VERSION)" \
		--build-arg APP_UID="$(BUILD_APP_UID)" \
		--build-arg APP_GID="$(BUILD_APP_GID)" \
		-f "$(DOCKERFILE)" \
		-t "$(IMAGE)" \
		"$(ROOT_DIR)"

build-no-cache:
	@test -f "$(ENV_FILE)" || \
		(echo "Build failed: $(ENV_FILE) is missing."; exit 1)
	DOCKER_BUILDKIT=1 docker build --no-cache \
		--build-arg PYTHON_VERSION="$(PYTHON_VERSION)" \
		--build-arg APP_UID="$(BUILD_APP_UID)" \
		--build-arg APP_GID="$(BUILD_APP_GID)" \
		-f "$(DOCKERFILE)" \
		-t "$(IMAGE)" \
		"$(ROOT_DIR)"

# ---------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------

up: preflight check-ports
	$(COMPOSE) up -d --no-build --remove-orphans

down:
	$(COMPOSE) down --remove-orphans

restart: preflight
	$(COMPOSE) up -d --force-recreate --no-build --remove-orphans

logs:
	$(COMPOSE) logs -f --tail=200

ps:
	$(COMPOSE) ps

health:
	@API_HEALTH_HOST="$(API_BIND_IP)"; \
	if [ "$$API_HEALTH_HOST" = "0.0.0.0" ] || \
	   [ "$$API_HEALTH_HOST" = "::" ] || \
	   [ "$$API_HEALTH_HOST" = "[::]" ]; then \
		API_HEALTH_HOST=127.0.0.1; \
	fi; \
	UI_HEALTH_HOST="$(UI_BIND_IP)"; \
	if [ "$$UI_HEALTH_HOST" = "0.0.0.0" ] || \
	   [ "$$UI_HEALTH_HOST" = "::" ] || \
	   [ "$$UI_HEALTH_HOST" = "[::]" ]; then \
		UI_HEALTH_HOST=127.0.0.1; \
	fi; \
	curl -fsS "http://$$API_HEALTH_HOST:$(API_PORT)/health"; echo; \
	curl -fsS "http://$$API_HEALTH_HOST:$(API_PORT)/openapi.json" >/dev/null; \
	echo "OpenAPI OK"; \
	curl -fsS "http://$$UI_HEALTH_HOST:$(UI_PORT)/_stcore/health"; echo

# ---------------------------------------------------------------------
# Image inspection and export
# ---------------------------------------------------------------------

inspect-image:
	@docker image inspect "$(IMAGE)" \
		--format '{{.Config.User}} {{.Config.WorkingDir}}'
	@docker run --rm --entrypoint python "$(IMAGE)" -c \
		'import importlib.metadata as metadata, torch; \
assert torch.version.cuda is None; \
assert not torch.cuda.is_available(); \
bad=[dist.metadata["Name"] for dist in metadata.distributions() \
if (dist.metadata.get("Name") or "").lower().startswith(("nvidia-", "cuda-"))]; \
assert not bad, bad; \
print("CPU Torch OK:", torch.__version__)'
	@docker run --rm --entrypoint sh "$(IMAGE)" -c \
		'test -z "$$(find /opt/venv -type f \( -name "*.whl" -o -name "*.tar.gz" \) -print -quit)"; \
test -z "$$(find /workspace/data -type f -print -quit)"; \
test -z "$$(find /home/soorin/.cache/huggingface -type f -print -quit)"; \
test ! -e /workspace/app/.env; \
test ! -e /workspace/.env; \
test ! -e /workspace/compose.env'
	@echo "Image content safeguards passed."

inspect-size:
	@docker run --rm --entrypoint sh "$(IMAGE)" -c \
		'du -sh /opt/venv; \
du -sh /opt/venv/lib/python3.12/site-packages/* 2>/dev/null | sort -h | tail -25'

export:
	@mkdir -p "$(EXPORT_DIR)"
	docker save \
		-o "$(EXPORT_DIR)/soorin-copilot-$(IMAGE_TAG).tar" \
		"$(IMAGE)"
	sha256sum \
		"$(EXPORT_DIR)/soorin-copilot-$(IMAGE_TAG).tar" \
		> "$(EXPORT_DIR)/soorin-copilot-$(IMAGE_TAG).tar.sha256"
	@echo "Exported to $(EXPORT_DIR)"

clean-export:
	rm -f \
		"$(EXPORT_DIR)/soorin-copilot-$(IMAGE_TAG).tar" \
		"$(EXPORT_DIR)/soorin-copilot-$(IMAGE_TAG).tar.sha256"
