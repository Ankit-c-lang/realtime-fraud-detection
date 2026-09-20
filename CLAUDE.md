# CLAUDE.md: realtime-fraud-detection

## What this is
Near-real-time transaction fraud monitoring on synthetic data:
simulator -> FeatureEngine (hot features) + graph snapshots (warm features)
-> XGBoost + Isolation Forest blend -> Redis Streams scorer -> Parquet/DuckDB, FastAPI, Streamlit.
The full plan is PLAN.md; prompts reference its section numbers.
Current status, next task and phase history live in PROGRESS.md.

## Environment (already provisioned — do not re-install)
The base environment is ready. Verified on 2026-09-20:

| Tool | Version | Status |
|---|---|---|
| Ubuntu | 24.04.4 LTS | ready |
| Python | 3.12.3 (system) | ready |
| uv | 0.12.17 | ready |
| Git | 2.43.0 (+ GitHub SSH key) | ready |
| Docker Engine | 29.8.0 (rootless-group, no sudo) | ready |
| Docker Compose | v5.5.1 (v2-style CLI) | ready |
| Claude Code | current | ready |

- Repo path: `/home/ankit/projects/realtime-fraud-detection` (VM's own disk, not a shared folder).
- `id -u` is **1000**, which matches the Dockerfile uid in PLAN §12.1 — bind mounts need no `user:` override.
- VM resources: 10 vCPUs, 7.7 GB RAM. This is below the 8-10 GB the plan hoped for (PLAN §15.2),
  so keep memory budgets tight and re-check `docker stats` peaks during Phase 9.
- CPU only (i5-1235U, integrated GPU). No GPU code, small tuning budgets.
- **Redis is the only missing piece and it comes from Docker Compose later** (Phase 0 adds the
  `redis` service; `make redis-up`). Do not apt-install a Redis server. `redis-tools` on the host
  is optional — `docker compose exec redis redis-cli ping` works without it.
- REDIS_URL is `redis://localhost:6379/0` for host-side commands, `redis://redis:6379/0` inside
  Compose. Tests use DB 15.

Never ask the user to install Ubuntu packages, uv, Git, Docker or Claude Code — they exist.
Anything else (a new Python dependency, a new container, a new service) needs explicit approval.

## Commands
- make setup | lint | format | test | test-redis | test-all
- make redis-up
- make data | features | graph | experiments | train V=v1 | evaluate-test V=v1 | all
- make up | demo | logs | demo-reset | smoke | bench | rescore-check

All Python targets run through `uv run`.

## Invariants (never break these)
1. Event time only; windows are half-open [t-w, t); compute features BEFORE updating state (PLAN §5.1).
2. Feature names and order come only from src/fraud/features/spec.py. Changing a definition means bumping FEATURE_SPEC_VERSION.
3. One FeatureEngine for offline and online; stores differ, logic does not (§5.4).
4. Labels never enter events, the stream or the scorer. Only evaluation code and graph seeds
   (label_available_at < T) read labels (§4.6, §6.2).
5. Graph snapshot T uses events < T; attach snapshots with the two-step join (§6.4), never ASOF on account.
6. Split dates live only in configs/splits.yaml. The test split is read only by evaluate_test.py with ALLOW_TEST=1 (§7.11).
7. Each data directory has exactly one writer. Readers use in-memory DuckDB over Parquet (§3.7, §9.4).
   Services never open a .duckdb file.
8. Scorer: the per-event commit is idempotent; XACK only after the sink flush (§9.2, §9.3).
9. The API's /score never writes state (§10).
10. Every number in README or CV comes from reports/ (§0.4).
11. **configs/sim.yaml is FROZEN at tag `sim-v1` (2026-09-20), sha256 `97404e57e5da143b1f1b47c11caac96d315ce5fe85da1f47feba5cda9976863f`.**
    Do not edit it, configs/sim_tiny.yaml, configs/categories.yaml, configs/cities.csv or
    configs/splits.yaml, and do not change simulator behaviour. PLAN §4.8 allows exactly one
    revision (`sim-v2`): only if validation PR-AUC exceeds 0.995 in Phase 3, only once, with the
    reason written in the README, and never after test metrics have been seen. Metrics are never
    improved by editing the generator.

## Out of scope (do not add)
MLflow, Optuna, CatBoost, LightGBM, PostgreSQL, Kafka, Spark, Airflow, Neo4j, GNNs, Kubernetes,
cloud deployment, auth, Prometheus/Grafana, PyOD, extra services.
Ask before adding ANY dependency.

## Conventions
- Type hints everywhere. Pure functions in features/ and graph/algorithms.py. I/O only in stores, storage/ and entrypoints.
- All config via src/fraud/config.py (YAML + env). No hard-coded paths, dates or thresholds.
- Deterministic: seeded RNGs, ORDER BY before building graphs, sorted JSON.
- Use the logging module; no print() in library code. Ruff for lint and format (line length 100).
- Python 3.12 is pinned in four places — `.python-version`, `requires-python`, the Dockerfile base
  image, CI `python-version` — plus this file. Change all of them together or not at all.

## Working agreement
- Before editing more than one file, show a short plan and the file list.
- Write or update tests in the same change. Run `make lint && make test`
  (plus `make test-redis` when Redis code changed) and show the output.
- Do not modify configs/sim.yaml, configs/splits.yaml or reports/ unless the task says so.
- Never state a metric or benchmark result; point to the command that produces it.
- If PLAN.md and the code disagree, stop and ask.
- **Update PROGRESS.md at the end of every completed task**, before or with the commit: move the
  item to Completed with its date, set the new "Currently working on", and set "Next up".
  A task is not done until PROGRESS.md reflects it.
