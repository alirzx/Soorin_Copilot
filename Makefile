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
EXPORT_DIR ?= $(ROOT_DIR)/dist

COMPOSE := docker compose \
	--project-directory "$(ROOT_DIR)" \
	-f "$(COMPOSE_FILE)" \
	--env-file