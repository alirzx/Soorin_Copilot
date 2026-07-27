SHELL := /bin/bash

COMPOSE_ENV ?= compose.env
COMPOSE := docker compose --env-file $(COMPOSE_ENV)
PYTHON ?= .venv/bin/python

IMAGE_TAG := $(or $(shell sed -n 's/^SOORIN_IMAGE_TAG=//p' $(COMPOSE_ENV) 2>/dev/null),dev-local)
IMAGE := soorin-copilot:$(IMAGE_TAG)

API_BIND_IP := $(or $(shell sed -n 's/^SOORIN_API_BIND_IP=//p' $(COMPOSE_ENV) 2>/dev/null),127.0.0.1)
API_PORT := $(or $(shell sed -n 's/^SOORIN_API_PORT=//p' $(COMPOSE_ENV) 2>/dev/null),6998)
UI_BIND_IP := $(or $(shell sed -n 's/^SOORIN_UI_BIND_IP=//p' $(COMPOSE_ENV) 2>/dev/null),127.0.0.1)
UI_PORT := $(or $(shell sed -n 's/^SOORIN_UI_PORT=//p' $(COMPOSE_ENV) 2>/dev/null),8503)

APP_UID := $(or $(shell sed -n 's/^APP_UID=//p' $(COMPOSE_ENV) 2>/dev/null),10001)
APP_GID := $(or $(shell sed -n 's/^APP_GID=//p' $(COMPOSE_ENV) 2>/dev/null),10001)

DATA_HOST_PATH := $(shell sed -n 's/^SOORIN_DATA_HOST_PATH=//p' $(COMPOSE_ENV) 2>/dev/null)
HF_CACHE_HOST_PATH := $(shell sed -n 's/^SOORIN_HF_CACHE_HOST_PATH=//p' $(COMPOSE_ENV) 2>/dev/null)
RAG_COLLECTION := $(shell sed -n 's/^SOORIN_RAG_COLLECTION=//p' app/.env 2>/dev/null)
RAG_MODEL := $(shell sed -n 's/^SOORIN_RAG_EMBEDDING_MODEL=//p' app/.env 2>/dev/null)
RAG_REVISION := $(shell sed -n 's/^SOORIN_RAG_EMBEDDING_REVISION=//p' app/.env 2>/dev/null)
RAG_MODEL_CACHE_KEY := models--$(subst /,--,$(RAG_MODEL))

.PHONY: \
	help preflight config test-local build build-no-cache \
	up down restart logs ps health inspect-image inspect-size export

help:
	@echo "Available targets:"
	@echo "  make preflight        Validate local deployment inputs without exposing secrets"
	@echo "  make config           Validate Compose configuration"
	@echo "  make test-local       Run the complete offline test suite in the local venv"
	@echo "  make build            Build using Docker cache"
	@echo "  make build-no-cache   Build without cache or registry pull"
	@echo "  make up               Start the existing image"
	@echo "  make down             Stop stack and preserve host data"
	@echo "  make restart          Recreate containers without rebuilding"
	@echo "  make logs             Follow container logs"
	@echo "  make ps               Show service status"
	@echo "  make health           Check API, OpenAPI and UI"
	@echo "  make inspect-image    Verify local image CPU/runtime/content contracts"
	@echo "  make inspect-size     Show the largest installed runtime packages"
	@echo "  make export           Export image and checksum"

preflight:
	@test -f app/.env || (echo "Preflight failed: app/.env is missing."; exit 1)
	@test -f $(COMPOSE_ENV) || (echo "Preflight failed: $(COMPOSE_ENV) is missing."; exit 1)
	@test -n "$(DATA_HOST_PATH)" && test -d "$(DATA_HOST_PATH)" || (echo "Preflight failed: configured data host path is missing."; exit 1)
	@test -n "$(HF_CACHE_HOST_PATH)" && test -d "$(HF_CACHE_HOST_PATH)" || (echo "Preflight failed: configured Hugging Face cache is missing."; exit 1)
	@$(PYTHON) -c 'import os, stat, sys; p=sys.argv[1]; uid=int(sys.argv[2]); gid=int(sys.argv[3]); s=os.stat(p); checks=((s.st_uid==uid,stat.S_IWUSR|stat.S_IXUSR),(s.st_gid==gid,stat.S_IWGRP|stat.S_IXGRP),(True,stat.S_IWOTH|stat.S_IXOTH)); ok=any(owner and s.st_mode & mask == mask for owner,mask in checks); sys.exit(0 if ok else "Preflight failed: data host path is not writable/traversable by configured APP_UID/APP_GID.")' "$(DATA_HOST_PATH)" "$(APP_UID)" "$(APP_GID)"
	@test -f "$(DATA_HOST_PATH)/qdrant-local/meta.json" || (echo "Preflight failed: qdrant-local/meta.json is missing."; exit 1)
	@$(PYTHON) -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); c=sys.argv[2]; sys.exit(0 if c and c in d.get("collections",{}) else "Preflight failed: configured Qdrant collection metadata is missing.")' "$(DATA_HOST_PATH)/qdrant-local/meta.json" "$(RAG_COLLECTION)"
	@test -f "$(DATA_HOST_PATH)/processed/topology_graph.pkl" || (echo "Preflight failed: topology_graph.pkl is missing."; exit 1)
	@test -f "$(DATA_HOST_PATH)/raw/topology_raw.json" || (echo "Preflight failed: topology_raw.json is missing."; exit 1)
	@test -f "$(DATA_HOST_PATH)/processed/topology_stats.json" || (echo "Preflight failed: topology_stats.json is missing."; exit 1)
	@test -n "$(RAG_REVISION)" && test -r "$(HF_CACHE_HOST_PATH)/hub/$(RAG_MODEL_CACHE_KEY)/snapshots/$(RAG_REVISION)/config.json" || (echo "Preflight failed: configured BGE model revision is not readable in the Hugging Face cache."; exit 1)
	@$(PYTHON) -c 'import os,stat,sys; uid=int(sys.argv[2]); gid=int(sys.argv[3]); paths=((sys.argv[1],stat.S_IRUSR|stat.S_IXUSR),(sys.argv[4],stat.S_IRUSR)); ok=all(any(match and s.st_mode & mask == mask for match,mask in ((s.st_uid==uid,need),(s.st_gid==gid,need>>3),(True,need>>6))) for path,need in paths for s in (os.stat(path),)); sys.exit(0 if ok else "Preflight failed: configured APP_UID/APP_GID cannot read the BGE cache.")' "$(HF_CACHE_HOST_PATH)" "$(APP_UID)" "$(APP_GID)" "$(HF_CACHE_HOST_PATH)/hub/$(RAG_MODEL_CACHE_KEY)/snapshots/$(RAG_REVISION)/config.json"
	@$(PYTHON) -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); sys.exit(0 if d.get("hidden_size")==768 else "Preflight failed: cached BGE model hidden_size is not 768.")' "$(HF_CACHE_HOST_PATH)/hub/$(RAG_MODEL_CACHE_KEY)/snapshots/$(RAG_REVISION)/config.json"
	@$(PYTHON) -c 'exec("import socket, sys\nfor host, raw_port in ((sys.argv[1], sys.argv[2]), (sys.argv[3], sys.argv[4])):\n    sock = socket.socket()\n    try:\n        sock.bind((host, int(raw_port)))\n    except OSError:\n        sys.exit(1)\n    finally:\n        sock.close()")' "$(API_BIND_IP)" "$(API_PORT)" "$(UI_BIND_IP)" "$(UI_PORT)" || (echo "Preflight failed: a required host port is unavailable."; exit 1)
	@$(COMPOSE) config --quiet
	@echo "Preflight passed."

config:
	@$(COMPOSE) config --quiet
	@echo "Compose configuration is valid."

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
	@curl -fsS http://127.0.0.1:$(API_PORT)/health
	@echo
	@curl -fsS http://127.0.0.1:$(API_PORT)/openapi.json >/dev/null
	@echo "OpenAPI OK"
	@curl -fsS http://127.0.0.1:$(UI_PORT)/_stcore/health
	@echo

inspect-image:
	@docker image inspect $(IMAGE) --format '{{.Config.User}} {{.Config.WorkingDir}}'
	@docker run --rm --entrypoint python $(IMAGE) -c 'import importlib.metadata as m, torch; assert torch.version.cuda is None; assert not torch.cuda.is_available(); bad=[d.metadata["Name"] for d in m.distributions() if (d.metadata.get("Name") or "").lower().startswith(("nvidia-","cuda-"))]; assert not bad,bad; print("CPU Torch OK:",torch.__version__)'
	@docker run --rm --entrypoint sh $(IMAGE) -c 'test -z "$$(find /opt/venv -type f \( -name "*.whl" -o -name "*.tar.gz" \) -print -quit)"; test -z "$$(find /workspace/data -type f -print -quit)"; test -z "$$(find /home/soorin/.cache/huggingface -type f -print -quit)"; test ! -e /workspace/app/.env; test ! -e /workspace/compose.env'
	@echo "Image content safeguards passed."

inspect-size:
	@docker run --rm --entrypoint sh $(IMAGE) -c 'du -sh /opt/venv; du -sh /opt/venv/lib/python3.12/site-packages/* 2>/dev/null | sort -h | tail -25'

export:
	docker save \
		-o soorin-copilot-$(IMAGE_TAG).tar \
		$(IMAGE)
	sha256sum \
		soorin-copilot-$(IMAGE_TAG).tar \
		> soorin-copilot-$(IMAGE_TAG).tar.sha256
