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
	$(RUN) python -m fraud.graph.snapshots
	$(RUN) python -m fraud.graph.join

experiments:  ## Run E1-E5 on the validation split (PLAN §7.9)
	$(RUN) python -m fraud.modeling.experiments

lopo:  ## Choose the blend weight by leave-one-pattern-out (PLAN §7.6)
	$(RUN) python -m fraud.modeling.blend

train:  ## Build the final model into models/<V> (PLAN §8)
	$(RUN) python -m fraud.modeling.artifacts --version $(V)

evaluate-test:  ## The single guarded test-set evaluation of models/<V> (PLAN §7.11)
	ALLOW_TEST=1 $(RUN) python -m fraud.modeling.evaluate_test --version $(V)
	$(MAKE) results

results:  ## Render reports/results.md and the contribution figure (PLAN §7.9)
	$(RUN) python scripts/export_results.py

all: data features graph train  ## The whole offline pipeline

# --- live stack -------------------------------------------------------------

up:  ## Build and start the stack (PLAN §12.2)
	# A fresh clone has no .env, so CONSUMER_NAME is unset and the scorer falls back to
	# its container hostname (PLAN §9.2). That works, but the id changes whenever the
	# container is recreated, and the previous consumer's pending messages are then
	# orphaned until the 60s-idle reclaim finds them instead of being drained at startup.
	@test -f .env || { cp .env.example .env; echo "created .env from .env.example"; }
	# Build ONE service, not `up --build`. Every service shares `image: fraud-app:local`,
	# but `--build` still starts a build job per service, and six concurrent jobs each
	# needing scratch space fill a 39 GB disk before the layer cache can help.
	docker compose build scorer
	docker compose up -d

demo:  ## Replay the test window through the stack (PLAN §12.2)
	docker compose --profile demo up replayer

logs:  ## Follow the scorer and graph-refresh logs
	docker compose logs -f scorer graph-refresh

demo-reset:  ## Tear the stack down and drop replay output
	docker compose --profile demo down -v
	rm -rf data/scored data/graph/live

smoke:  ## End-to-end check: replay 2,000 events and assert the output
	docker compose build scorer
	docker compose up -d redis backfill scorer graph-refresh api
	docker compose run --rm --no-deps replayer python -m fraud.stream.replayer --max --limit 2000
	$(RUN) python scripts/smoke_check.py --expect 2000

bench:  ## Throughput and latency benchmark (PLAN §9.6)
	$(RUN) python scripts/benchmark_stream.py

bench-docker:  ## Sample container memory during a Compose replay (PLAN §12.2)
	# `make bench` runs the scorer in-process, so docker stats would show idle
	# containers. This measures the real services instead.
	@bash scripts/capture_container_memory.sh

rescore-check:  ## Re-score live output offline and compare (PLAN §9.6)
	$(RUN) python scripts/rescore_check.py

backfill:  ## Load the checkpoint and first snapshot into Redis (PLAN §9.5)
	$(RUN) python -m fraud.stream.backfill

graph-refresh:  ## Publish live graph snapshots while the replay runs (PLAN §6.5)
	$(RUN) python -m fraud.graph.refresh_live

api:  ## Run the FastAPI service on :8000 (PLAN §10)
	$(RUN) uvicorn fraud.api.main:app --host 0.0.0.0 --port 8000

dashboard:  ## Run the Streamlit dashboard on :8501 (PLAN §11)
	$(RUN) streamlit run dashboard/app.py --server.port 8501 --server.headless true
