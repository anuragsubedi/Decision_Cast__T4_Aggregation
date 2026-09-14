PY := .venv/bin/python
PORT ?= 8901

.DEFAULT_GOAL := help

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Create the venv and install the project (editable)
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -e ".[dev]"
	@test -f .env || echo "No .env found -- see tier4/config.py for the settings, TIER4_BE_EMAIL and TIER4_BE_PASSWORD are required"
	@echo "Ready. Edit .env if your backend is not on https://localhost:8855"

health:  ## Confirm identity and backend reachability
	$(PY) -m tier4.cli health

methods:  ## List the methods this worker can execute
	$(PY) -m tier4.cli methods

once:  ## Drain the pending task queue exactly once
	$(PY) -m tier4.cli once

poll:  ## Drain the queue continuously
	$(PY) -m tier4.cli poll

seed:  ## Create a question with published answers + an aggregation request
	$(PY) -m fixtures.seed_pilot_question

dev:  ## Run the API with auto-reload (NOT debuggable -- see debug target)
	$(PY) -m uvicorn tier4.app:app --reload --port $(PORT)

serve:  ## Run the API without reload (production-shaped, debugger-friendly)
	$(PY) -m uvicorn tier4.app:app --host 0.0.0.0 --port $(PORT)

debug:  ## Run the API waiting for a debugger to attach on :5678
	$(PY) -m debugpy --listen 5678 --wait-for-client \
		-m uvicorn tier4.app:app --port $(PORT)

test:  ## Run the test suite (no backend required)
	$(PY) -m pytest tests/ -q

docs:  ## Print the URLs of the auto-generated API docs
	@echo "Swagger UI:  http://localhost:$(PORT)/docs"
	@echo "ReDoc:       http://localhost:$(PORT)/redoc"
	@echo "OpenAPI:     http://localhost:$(PORT)/openapi.json"

.PHONY: help install health methods once poll seed dev serve debug test docs
