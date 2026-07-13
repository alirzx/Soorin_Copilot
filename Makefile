SHELL := /bin/bash

COMPOSE := docker compose
IMAGE := soorin-copilot
TAG ?= local

.PHONY: help build up down restart logs ps health test config clean

help:
	@echo "Available targets:"
	@echo "  make build    Build the Copilot image"
	@echo "  make up       Start API and Streamlit UI"
	@echo "  make down     Stop containers"
	@echo "  make restart  Restart the stack"
	@echo "  make logs     Follow container logs"
	@echo "  make ps       Show service status"
	@echo "  make health   Check API and UI health"
	@echo "  make test     Run tests inside the image"
	@echo "  make config   Validate Compose configuration"
	@echo "  make clean    Remove containers and local image"

build:
	DOCKER_BUILDKIT=1 $(COMPOSE) build

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) down
	$(COMPOSE) up -d

logs:
	$(COMPOSE) logs -f --tail=200

ps:
	$(COMPOSE) ps

health:
	@curl -fsS http://127.0.0.1:$${SOORIN_API_PORT:-6998}/health
	@echo
	@curl -fsS http://127.0.0.1:$${SOORIN_UI_PORT:-8501}/_stcore/health
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

clean:
	$(COMPOSE) down --volumes --remove-orphans
	docker image rm $(IMAGE):$(TAG) 2>/dev/null || true