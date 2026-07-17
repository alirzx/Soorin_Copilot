SHELL := /bin/bash

COMPOSE := docker compose
COMPOSE_ENV ?= compose.env

IMAGE := soorin-copilot
TAG ?= local

DC := SOORIN_IMAGE_TAG=$(TAG) $(COMPOSE) --env-file $(COMPOSE_ENV)

.PHONY: \
	help build build-no-cache up down restart logs ps health \
	test config clean purge export

help:
	@echo "Available targets:"
	@echo "  make build            Build using Docker cache"
	@echo "  make build-no-cache   Build completely without cache"
	@echo "  make up               Start the existing image"
	@echo "  make down             Remove containers/network; keep volumes"
	@echo "  make restart          Recreate containers without rebuilding"
	@echo "  make logs             Follow logs"
	@echo "  make ps               Show service status"
	@echo "  make health           Verify API, OpenAPI, UI, and source file"
	@echo "  make test             Run tests inside the image"
	@echo "  make config           Validate Compose"
	@echo "  make clean            Remove stack and image; keep volumes"
	@echo "  make purge            Remove stack, image, and volumes"
	@echo "  make export           Export image to tar"

build:
	DOCKER_BUILDKIT=1 $(DC) build api

build-no-cache:
	DOCKER_BUILDKIT=1 $(DC) build --no-cache --pull api

up:
	$(DC) up -d --force-recreate --no-build

down:
	$(DC) down --remove-orphans

restart:
	$(DC) up -d --force-recreate --no-build

logs:
	$(DC) logs -f --tail=200

ps:
	$(DC) ps

health:
	@curl -fsS http://127.0.0.1:$${SOORIN_API_PORT:-6998}/health
	@echo
	@curl -fsS http://127.0.0.1:$${SOORIN_API_PORT:-6998}/openapi.json >/dev/null
	@echo "OpenAPI OK"
	@curl -fsS http://127.0.0.1:$${SOORIN_UI_PORT:-8503}/_stcore/health
	@echo
	@docker exec soorin-copilot-ui \
		python -c 'from pathlib import Path; print(Path("/workspace/app/app_st.py").stat())'

test:
	$(DC) run --rm --no-deps api \
		python -m unittest discover \
		-s app/src/tests \
		-p "test_*.py" \
		-v

config:
	$(DC) config --quiet
	@echo "Compose configuration is valid."

clean:
	$(DC) down --remove-orphans
	docker image rm $(IMAGE):$(TAG) 2>/dev/null || true
	docker image prune -f

purge:
	$(DC) down --volumes --remove-orphans
	docker image rm $(IMAGE):$(TAG) 2>/dev/null || true
	docker image prune -f

export:
	docker save \
		-o $(IMAGE)-$(TAG).tar \
		$(IMAGE):$(TAG)