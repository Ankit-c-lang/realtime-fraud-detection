# Makefile targets follow PLAN.md §16.1. All Python runs through uv.
# Targets for phases that are not built yet fail loudly instead of doing nothing.

UV ?= uv
RUN := $(UV) run
REDIS_URL ?= redis://localhost:6379/0
# Integration tests get their own database so they never flush live scorer state (PLAN §13).
TEST_REDIS_URL ?= redis://localhost:6379/15
V ?= v1
export REDIS_URL

.DEFAULT_GOAL := help

.PHONY: help setup lint format test test-redis test-all redis-up \
        data features graph experiments train evaluate-test all \
        up demo logs demo-reset smoke bench rescore-check

help:  ## List the available targets
	@grep -hE '^[a-z][a-z-]*:.*## ' $(MAKEFILE_LIST) | sed -e 's/:.*## /\t/' | expand -t 18

# --- environment and quality gates (Phase 0) --------------------------------

setup:  ## Install every dependency group into .venv
	$(UV) sync --all-groups

lint:  ## Ruff check and format check
	$(RUN) ruff check .
	$(RUN) ruff format --check .

format:  ## Ruff auto-fix and format
	$(RUN) ruff check --fix .
	$(RUN) ruff format .

test:  ## Fast unit tests (no Redis, nothing slow)
	REDIS_URL=$(TEST_REDIS_URL) $(RUN) pytest -m "not redis and not slow"

test-redis:  ## Integration tests that need a Redis server
	REDIS_URL=$(TEST_REDIS_URL) $(RUN) pytest -m redis

test-all:  ## The whole suite
	REDIS_URL=$(TEST_REDIS_URL) $(RUN) pytest

redis-up:  ## Start the Redis container
	docker compose up -d redis

# --- offline pipeline -------------------------------------------------------

data:  ## Generate the simulated dataset and its report (PLAN §4)
	$(RUN) python -m fraud.sim.generate
	$(RUN) python -m fraud.sim.checks

features:  ## Offline replay to hot_features.parquet plus the checkpoint (PLAN §5)
	$(RUN) python -m fraud.features.replay

graph:  ## Build graph snapshots and the point-in-time join (PLAN §6)
	@echo "not implemented yet: $@ — Phase 4, PLAN §6"; exit 1

experiments:  ## Run E1-E5 on the validation split (PLAN §7.9)
	$(RUN) python -m fraud.modeling.experiments

train:  ## Build the final model into models/<V> (PLAN §8)
	@echo "not implemented yet: $@ — Phase 5, PLAN §8"; exit 1

evaluate-test:  ## The single guarded test-set evaluation of models/<V> (PLAN §7.11)
	@echo "not implemented yet: $@ — Phase 5, PLAN §7.11"; exit 1

all: data features graph train  ## The whole offline pipeline

# --- live stack -------------------------------------------------------------

up:  ## Build and start the stack (PLAN §12.2)
	@echo "not implemented yet: $@ — Phase 9, PLAN §12.2"; exit 1

demo:  ## Replay the test window through the stack (PLAN §12.2)
	@echo "not implemented yet: $@ — Phase 9, PLAN §12.2"; exit 1

logs:  ## Follow the scorer and graph-refresh logs
	@echo "not implemented yet: $@ — Phase 9, PLAN §12.2"; exit 1

demo-reset:  ## Tear the stack down and drop replay output
	@echo "not implemented yet: $@ — Phase 9, PLAN §12.2"; exit 1

smoke:  ## End-to-end check: replay 2,000 events and assert the output
	@echo "not implemented yet: $@ — Phase 9, PLAN §12.2"; exit 1

bench:  ## Throughput and latency benchmark (PLAN §9.6)
	@echo "not implemented yet: $@ — Phase 9, PLAN §9.6"; exit 1

rescore-check:  ## Re-score live output offline and compare (PLAN §9.6)
	@echo "not implemented yet: $@ — Phase 6, PLAN §9.6"; exit 1
