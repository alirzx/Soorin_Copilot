SHELL := /bin/bash

# ---------------------------------------------------------------------
# Deployment inputs
# ---------------------------------------------------------------------

COMPOSE_ENV ?= compose.env
COMPOSE := docker compose --env-file "$(COMPOSE_ENV)"

# Prefer the project virtual environment locally; fall back to system
# Python on deployment hosts where no .venv exists.
PYTHON ?= $(shell \
	if [ -x ".venv/bin/python" ]; then \
		printf '%s' ".venv/bin/python"; \
	elif command -v python3 >/dev/null 2>&1; then \
		command -v python3; \
	else \
		printf '%s' "python"; \
	fi \
)

# Read a key from compose.env without exporting or printing secrets.
define compose_env_value
$(strip $(shell sed -n 's/^$(1)=//p' "$(COMPOSE_ENV)" 2>/dev/null | tail -n 1))
endef

IMAGE_TAG := $(or $(call compose_env_value,SOORIN_IMAGE_TAG),dev-local)
IMAGE := soorin-copilot:$(IMAGE_TAG)

API_BIND_IP := $(or $(call compose_env_value,SOORIN_API_BIND_IP),127.0.0.1)
API_PORT := $(or $(call compose_env_value,SOORIN_API_PORT),6998)
UI_BIND_IP := $(or $(call compose_env_value,SOORIN_UI_BIND_IP),127.0.0.1)
UI_PORT := $(or $(call compose_env_value,SOORIN_UI_PORT),8503)

APP_UID := $(or $(call compose_env_value,APP_UID),10001)
APP_GID := $(or $(call compose_env_value,APP_GID),10001)

APP_ENV_FILE := $(or $(call compose_env_value,SOORIN_APP_ENV_FILE),./app/.env)
DATA_HOST_PATH := $(call compose_env_value,SOORIN_DATA_HOST_PATH)
HF_CACHE_HOST_PATH := $(call compose_env_value,SOORIN_HF_CACHE_HOST_PATH)

# Read non-secret RAG deployment metadata from the configured application
# environment file. APP_ENV_FILE may be relative or absolute.
RAG_COLLECTION := $(strip $(shell \
	sed -n 's/^SOORIN_RAG_COLLECTION=//p' "$(APP_ENV_FILE)" 2>/dev/null | tail -n 1 \
))
RAG_MODEL := $(strip $(shell \
	sed -n 's/^SOORIN_RAG_EMBEDDING_MODEL=//p' "$(APP_ENV_FILE)" 2>/dev/null | tail -n 1 \
))
RAG_REVISION := $(strip $(shell \
	sed -n 's/^SOORIN_RAG_EMBEDDING_REVISION=//p' "$(APP_ENV_FILE)" 2>/dev/null | tail -n 1 \
))
RAG_MODEL_CACHE_KEY := models--$(subst /,--,$(RAG_MODEL))
RAG_CONFIG_PATH := $(HF_CACHE_HOST_PATH)/hub/$(RAG_MODEL_CACHE_KEY)/snapshots/$(RAG_REVISION)/config.json

.PHONY: \
	help preflight check-ports config test-local \
	build build-no-cache up down restart logs ps health \
	inspect-image inspect-size export

# ---------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------

help:
	@echo "Available targets:"
	@echo "  make preflight        Validate deployment files, mounts, RAG data and permissions"
	@echo "  make check-ports      Verify configured API and UI host ports are available"
	@echo "  make config           Validate the resolved Compose configuration"
	@echo "  make test-local       Run the complete offline test suite"
	@echo "  make build            Build the image using Docker cache"
	@echo "  make build-no-cache   Build the image without Docker cache"
	@echo "  make up               Recreate and start containers without rebuilding"
	@echo "  make down             Stop containers while preserving host-mounted data"
	@echo "  make restart          Recreate containers without rebuilding"
	@echo "  make logs             Follow API and UI logs"
	@echo "  make ps               Show Compose service status"
	@echo "  make health           Check API, OpenAPI and Streamlit health"
	@echo "  make inspect-image    Verify CPU-only and image-content contracts"
	@echo "  make inspect-size     Show the largest installed runtime packages"
	@echo "  make export           Export the image and generate its SHA-256 checksum"
	@echo
	@echo "Configuration:"
	@echo "  COMPOSE_ENV=$(COMPOSE_ENV)"
	@echo "  APP_ENV_FILE=$(APP_ENV_FILE)"
	@echo "  IMAGE=$(IMAGE)"

# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------

preflight:
	@test -f "$(COMPOSE_ENV)" || \
		(echo "Preflight failed: $(COMPOSE_ENV) is missing."; exit 1)
	@test -f "$(APP_ENV_FILE)" || \
		(echo "Preflight failed: $(APP_ENV_FILE) is missing."; exit 1)
	@command -v docker >/dev/null 2>&1 || \
		(echo "Preflight failed: docker is unavailable."; exit 1)
	@docker compose version >/dev/null 2>&1 || \
		(echo "Preflight failed: Docker Compose v2 is unavailable."; exit 1)
	@test -x "$(PYTHON)" || command -v "$(PYTHON)" >/dev/null 2>&1 || \
		(echo "Preflight failed: Python interpreter is unavailable: $(PYTHON)"; exit 1)

	@test -n "$(DATA_HOST_PATH)" || \
		(echo "Preflight failed: SOORIN_DATA_HOST_PATH is not configured."; exit 1)
	@test -d "$(DATA_HOST_PATH)" || \
		(echo "Preflight failed: data host path does not exist: $(DATA_HOST_PATH)"; exit 1)

	@test -n "$(HF_CACHE_HOST_PATH)" || \
		(echo "Preflight failed: SOORIN_HF_CACHE_HOST_PATH is not configured."; exit 1)
	@test -d "$(HF_CACHE_HOST_PATH)" || \
		(echo "Preflight failed: Hugging Face cache does not exist: $(HF_CACHE_HOST_PATH)"; exit 1)

	@test -n "$(RAG_COLLECTION)" || \
		(echo "Preflight failed: SOORIN_RAG_COLLECTION is missing from $(APP_ENV_FILE)."; exit 1)
	@test -n "$(RAG_MODEL)" || \
		(echo "Preflight failed: SOORIN_RAG_EMBEDDING_MODEL is missing from $(APP_ENV_FILE)."; exit 1)
	@test -n "$(RAG_REVISION)" || \
		(echo "Preflight failed: SOORIN_RAG_EMBEDDING_REVISION is missing from $(APP_ENV_FILE)."; exit 1)

	@$(PYTHON) -c 'import os, stat, sys; \
p=sys.argv[1]; uid=int(sys.argv[2]); gid=int(sys.argv[3]); s=os.stat(p); \
checks=((s.st_uid==uid, stat.S_IWUSR|stat.S_IXUSR), \
(s.st_gid==gid, stat.S_IWGRP|stat.S_IXGRP), \
(True, stat.S_IWOTH|stat.S_IXOTH)); \
ok=any(owner and (s.st_mode & mask)==mask for owner,mask in checks); \
sys.exit(0 if ok else "Preflight failed: data path is not writable/traversable by APP_UID/APP_GID.")' \
		"$(DATA_HOST_PATH)" "$(APP_UID)" "$(APP_GID)"

	@test -f "$(DATA_HOST_PATH)/qdrant-local/meta.json" || \
		(echo "Preflight failed: qdrant-local/meta.json is missing."; exit 1)

	@$(PYTHON) -c 'import json, sys; \
data=json.load(open(sys.argv[1], encoding="utf-8")); collection=sys.argv[2]; \
sys.exit(0 if collection in data.get("collections", {}) \
else "Preflight failed: configured Qdrant collection is absent from metadata.")' \
		"$(DATA_HOST_PATH)/qdrant-local/meta.json" "$(RAG_COLLECTION)"

	@test -f "$(DATA_HOST_PATH)/processed/topology_graph.pkl" || \
		(echo "Preflight failed: processed/topology_graph.pkl is missing."; exit 1)
	@test -f "$(DATA_HOST_PATH)/raw/topology_raw.json" || \
		(echo "Preflight failed: raw/topology_raw.json is missing."; exit 1)
	@test -f "$(DATA_HOST_PATH)/processed/topology_stats.json" || \
		(echo "Preflight failed: processed/topology_stats.json is missing."; exit 1)

	@test -r "$(RAG_CONFIG_PATH)" || \
		(echo "Preflight failed: cached embedding model revision is unreadable: $(RAG_CONFIG_PATH)"; exit 1)

	@$(PYTHON) -c 'import json, sys; \
data=json.load(open(sys.argv[1], encoding="utf-8")); \
sys.exit(0 if int(data.get("hidden_size", 0)) == 768 \
else "Preflight failed: cached embedding model hidden_size is not 768.")' \
		"$(RAG_CONFIG_PATH)"

	@$(PYTHON) -c 'import os, stat, sys; \
root=sys.argv[1]; config=sys.argv[2]; uid=int(sys.argv[3]); gid=int(sys.argv[4]); \
def allowed(path, owner_mask, group_mask, other_mask): \
 s=os.stat(path); \
 return ((s.st_uid==uid and (s.st_mode & owner_mask)==owner_mask) or \
         (s.st_gid==gid and (s.st_mode & group_mask)==group_mask) or \
         ((s.st_mode & other_mask)==other_mask)); \
ok=allowed(root, stat.S_IRUSR|stat.S_IXUSR, stat.S_IRGRP|stat.S_IXGRP, stat.S_IROTH|stat.S_IXOTH) \
and allowed(config, stat.S_IRUSR, stat.S_IRGRP, stat.S_IROTH); \
sys.exit(0 if ok else "Preflight failed: APP_UID/APP_GID cannot read the Hugging Face cache.")' \
		"$(HF_CACHE_HOST_PATH)" "$(RAG_CONFIG_PATH)" "$(APP_UID)" "$(APP_GID)"

	@$(COMPOSE) config --quiet
	@echo "Preflight passed."

# Run this before starting containers. It is intentionally separate from
# preflight because occupied ports are expected while the stack is running.
check-ports:
	@$(PYTHON) -c 'import socket, sys; \
pairs=((sys.argv[1], int(sys.argv[2]), "API"), (sys.argv[3], int(sys.argv[4]), "UI")); \
failed=[]; \
exec("for host, port, name in pairs:\n" \
"    sock=socket.socket()\n" \
"    try:\n" \
"        sock.bind((host, port))\n" \
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
# Testing and lifecycle
# ---------------------------------------------------------------------

test-local:
	PYTHONPATH=app $(PYTHON) -m pytest -q app/src/tests

build:
	DOCKER_BUILDKIT=1 $(COMPOSE) build api

build-no-cache:
	DOCKER_BUILDKIT=1 $(COMPOSE) build --no-cache api

up:
	$(COMPOSE) up -d --force-recreate --no-build

down:
	$(COMPOSE) down --remove-orphans

restart:
	$(COMPOSE) up -d --force-recreate --no-build

logs:
	$(COMPOSE) logs -f --tail=200

ps:
	$(COMPOSE) ps

health:
	@curl -fsS "http://127.0.0.1:$(API_PORT)/health"
	@echo
	@curl -fsS "http://127.0.0.1:$(API_PORT)/openapi.json" >/dev/null
	@echo "OpenAPI OK"
	@curl -fsS "http://127.0.0.1:$(UI_PORT)/_stcore/health"
	@echo

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
test ! -e /workspace/compose.env'
	@echo "Image content safeguards passed."

inspect-size:
	@docker run --rm --entrypoint sh "$(IMAGE)" -c \
		'du -sh /opt/venv; \
du -sh /opt/venv/lib/python3.12/site-packages/* 2>/dev/null | sort -h | tail -25'

export:
	docker save \
		-o "soorin-copilot-$(IMAGE_TAG).tar" \
		"$(IMAGE)"
	sha256sum \
		"soorin-copilot-$(IMAGE_TAG).tar" \
		> "soorin-copilot-$(IMAGE_TAG).tar.sha256"
