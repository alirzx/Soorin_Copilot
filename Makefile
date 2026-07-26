SHELL := /bin/bash

COMPOSE_ENV ?= compose.env
COMPOSE := docker compose --env-file $(COMPOSE_ENV)

IMAGE_TAG := $(or $(shell sed -n 's/^SOORIN_IMAGE_TAG=//p' $(COMPOSE_ENV)),rag-local)
IMAGE := soorin-copilot:$(IMAGE_TAG)

API_PORT := $(or $(shell sed -n 's/^SOORIN_API_PORT=//p' $(COMPOSE_ENV)),6998)
UI_PORT := $(or $(shell sed -n 's/^SOORIN_UI_PORT=//p' $(COMPOSE_ENV)),8503)

APP_UID := $(or $(shell sed -n 's/^APP_UID=//p' $(COMPOSE_ENV)),10001)
APP_GID := $(or $(shell sed -n 's/^APP_GID=//p' $(COMPOSE_ENV)),10001)

QDRANT_VOLUME := soorin-copilot_copilot-qdrant

.PHONY: \
	help build build-no-cache seed-qdrant \
	up down restart logs ps health test config inspect-image export

help:
	@echo "Available targets:"
	@echo "  make build            Build using Docker cache"
	@echo "  make build-no-cache   Build without cache or registry pull"
	@echo "  make seed-qdrant      Copy data/qdrant-local into the Qdrant volume"
	@echo "  make up               Start the existing image"
	@echo "  make down             Stop stack and preserve volumes"
	@echo "  make restart          Recreate containers without rebuilding"
	@echo "  make logs             Follow container logs"
	@echo "  make ps               Show service status"
	@echo "  make health           Check API, OpenAPI and UI"
	@echo "  make test             Run tests inside the image"
	@echo "  make config           Validate Compose configuration"
	@echo "  make inspect-image    Verify local image CPU/runtime contracts"
	@echo "  make export           Export image and checksum"

build:
	DOCKER_BUILDKIT=1 $(COMPOSE) build api

build-no-cache:
	DOCKER_BUILDKIT=1 $(COMPOSE) build --no-cache api

seed-qdrant:
	@test -d data/qdrant-local || \
		(echo "Missing data/qdrant-local"; exit 1)
	@docker volume create $(QDRANT_VOLUME) >/dev/null
	docker run --rm \
		--user 0:0 \
		-v "$(CURDIR)/data/qdrant-local:/source:ro" \
		-v "$(QDRANT_VOLUME):/destination" \
		--entrypoint sh \
		$(IMAGE) \
		-lc ' \
			find /destination \
				-mindepth 1 \
				-maxdepth 1 \
				-exec rm -rf -- {} +; \
			cp -a /source/. /destination/; \
			chown -R $(APP_UID):$(APP_GID) /destination; \
			du -sh /destination \
		'

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

test:
	$(COMPOSE) run --rm --no-deps api \
		python -m unittest discover \
		-s app/src/tests \
		-p "test_*.py" \
		-v

config:
	$(COMPOSE) config --quiet
	@echo "Compose configuration is valid."

inspect-image:
	docker image inspect $(IMAGE) --format '{{.Config.User}} {{.Config.WorkingDir}}'
	docker run --rm --entrypoint python $(IMAGE) -c 'import torch; assert torch.version.cuda is None; assert "+cpu" in torch.__version__; print(torch.__version__)'

export:
	docker save \
		-o soorin-copilot-$(IMAGE_TAG).tar \
		$(IMAGE)
	sha256sum \
		soorin-copilot-$(IMAGE_TAG).tar \
		> soorin-copilot-$(IMAGE_TAG).tar.sha256
