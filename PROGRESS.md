# PROGRESS — realtime-fraud-detection

Living status file. **Update it at the end of every completed task**, together with the commit for
that task. Source of truth for *what* to build is `PLAN.md`; this file only tracks *where we are*.

- **Plan version:** v1 (2026-09-16) · **Started:** 2026-09-20
- **Current phase:** Phase 0 — Environment and skeleton (PLAN §17, Phase 0)
- **Overall:** 6 / 73 must-have tasks done (+ 7 nice-to-have, not counted) · Phase 0: 6 / 7

---

## Currently working on

**Phase 0 · Task 6 — CI workflow (PLAN §14).**
Add `.github/workflows/ci.yml` with the `lint-unit` job only: checkout, setup-uv pinned to Python
3.12, `uv sync --frozen --all-groups`, `ruff check`, `ruff format --check`,
`pytest -m "not redis and not slow"`. Confirm it goes green on GitHub.

**Blocked:** needs the GitHub remote (task 2, below) before CI can run anywhere.

---

## Next up (in order)

1. **P0.2b** — create the GitHub repo and push `main` (needs the user: repo name and visibility).
2. **P0.6** — `.github/workflows/ci.yml` (`lint-unit` job only); CI green on GitHub.
3. Close Phase 0, tag `phase-0`, then start **Phase 1 (Simulator)** — `configs/` + `sim/population.py`.

---

## Environment (done — not part of the build work)

Verified on the VM on 2026-09-20; nothing here needs installing again.

- [x] Ubuntu 24.04.4 LTS
- [x] Python 3.12.3
- [x] uv 0.12.17
- [x] Git 2.43.0 + GitHub SSH key
- [x] Docker Engine 29.8.0 (no `sudo` needed) + Docker Compose v5.5.1
- [x] Claude Code
- [x] Repo on the VM's own disk: `/home/ankit/projects/realtime-fraud-detection`
- [x] `id -u` = 1000 (matches the Dockerfile uid in PLAN §12.1)
- [ ] **Redis — deliberately not installed. It arrives as a Docker Compose service in Phase 0 task 5.**

⚠️ VM has 7.7 GB RAM / 10 vCPUs. PLAN §15.2 asked for 8-10 GB. Not a blocker, but watch memory in
Phase 4 (graph snapshots) and Phase 9 (`docker stats` peaks in `reports/benchmark.md`).

---

## Completed

| Date | Phase | Work | Evidence |
|---|---|---|---|
| 2026-09-20 | 0 | Environment verified end to end (§15.1 checks) | table above |
| 2026-09-20 | 0 | `CLAUDE.md` created from PLAN §18.2 + this `PROGRESS.md` | `CLAUDE.md`, `PROGRESS.md` |
| 2026-09-20 | 0 | Repo skeleton from §16: 49 stub modules, `pyproject.toml` (§15.3), `.python-version`, Makefile (§16.1), ignore files, `.env.example`, placeholder `README.md`, `src/fraud/config.py`, `tests/` | `make lint` clean, `make test` 8 passed |
| 2026-09-20 | 0 | `uv lock` + `uv sync --all-groups` — 111 packages resolved, Python 3.12.3 | `uv.lock` committed |
| 2026-09-20 | 0 | `git init` + first two commits (plan/context, then skeleton) | `git log`: 2369922, af2cc96 |
| 2026-09-20 | 0 | `docker-compose.yml` with the `redis` service only, pinned to `redis:8.10.1-alpine`; container healthy | `docker compose exec redis redis-cli ping` → `PONG` |

---

## Remaining work

Checkpoints are hard gates (PLAN §2.4). If one is missed, apply the cut list in §2.4 **in order**
before continuing — do not silently slip.

### Phase 0 — Environment and skeleton (Day 1 am, ~4 h, 2/10) — IN PROGRESS
- [x] 1. Verify environment and VM allocation (§15.1)
- [x] 7a. `CLAUDE.md` from §18.2
- [~] 2. `git init` done and `PLAN.md` committed; **GitHub repo + push still outstanding**
- [x] 3. Skeleton from §16: empty modules w/ docstrings, `pyproject.toml` (§15.3), `.python-version`, Makefile stubs, `.gitignore`, `.dockerignore`, `.env.example`, `src/fraud/config.py`
- [x] 4. `uv lock && uv sync --all-groups`
- [x] 5. `docker-compose.yml` with the `redis` service only; `make redis-up` → `PONG`
- [ ] 6. `.github/workflows/ci.yml` (`lint-unit`) + `tests/test_smoke.py`
- [ ] 7b. Commit + push; `make setup && make lint && make test` green; tag `phase-0`

### Phase 1 — Simulator (Day 1 pm → Day 3 midday, ~16 h, 5/10)
- [ ] 1. Configs: `sim.yaml`, `sim_tiny.yaml`, `cities.csv` (15 IN + 8 intl), `categories.yaml`, `splits.yaml`
- [ ] 2. `sim/population.py` — accounts, merchants, devices, IP pools
- [ ] 3. `sim/legit.py` — legitimate behaviour + all hard negatives (§4.4)
- [ ] 4. `sim/patterns.py` — 4 fraud injectors + ring constraints (§4.5)
- [ ] 5. `sim/generate.py` — assemble, stable sort, `txn_id`, Parquet + `manifest.json`
- [ ] 6. `sim/checks.py` — §4.8 checks → `reports/sim_report.md`
- [ ] 7. `test_sim_*` tests
- [ ] 8. **Freeze** `sim.yaml` (hash in report), commit, tag `sim-v1`
- **Gate:** ~500K events, 1.2-1.8% fraud, ≥12 rings start in test window (≥4 reusing a device), same seed → same hashes

### Phase 2 — Feature engine and offline replay (Day 3 pm → Day 5 am, ~14 h, 6/10)
- [ ] 1. `features/spec.py` — `FEATURE_NAMES`, defaults, `FEATURE_SPEC_VERSION = "fs1"`
- [ ] 2. `features/state.py` — `AccountState` with exact JSON round trip
- [ ] 3. `features/accounts.py` — `AccountDirectory`
- [ ] 4. `features/engine.py` — `compute_features()`, `update_account()`, `FeatureEngine.process()`
- [ ] 5. `features/store_memory.py` — blobs + windowed entity indexes
- [ ] 6. `features/replay.py` — `hot_features.parquet` + checkpoint at `test_start`
- [ ] 7. `test_features_*`, `test_state_serialization.py`, `test_replay.py` (hand-computed values)
- **Gate:** `make features` < ~3 min, no NaNs, row count == event count, checkpoint round-trips

### Phase 3 — Splits, baselines, XGBoost (Day 5 → Day 6 am, ~8 h, 4/10)
- [ ] 1. `modeling/splits.py` (burn-in excluded, test behind `ALLOW_TEST`)
- [ ] 2. `modeling/metrics.py` (PR-AUC, capacity operating point, per-pattern recall, VDR, FPR)
- [ ] 3. `modeling/rules.py` — R1-R4 → **E1**
- [ ] 4. `modeling/train_xgb.py` — 20-config random search on hot features → **E2**, save param set P
- [ ] 5. `modeling/experiments.py` — writes `reports/experiments/E*.json`
- [ ] 6. `test_splits.py`, `test_metrics.py`, `test_rules.py`, `test_training_smoke.py` (part 1)
- **🚩 CHECKPOINT (end of Day 5): E2 on valid exists.** E2 must beat E1 on PR-AUC and recall@budget.

### Phase 4 — Graph layer (Day 6 pm → Day 7, ~14 h, 6/10)
- [ ] 1. Measure accounts-per-device / per-IP on train; set `device_cap`, `ip_cap` in `graph.yaml`
- [ ] 2. `graph/projection.py` (§6.1 SQL, with `ORDER BY`)
- [ ] 3. `graph/algorithms.py` — degree, clustering, seeded Louvain, community aggregates, personalized PageRank w/ label delay
- [ ] 4. `graph/snapshots.py` — daily offline snapshots + `calendar.parquet`
- [ ] 5. `graph/join.py` — two-step point-in-time join → `training_table.parquet`
- [ ] 6. **E3** (36 features, param set P) + ablation vs E2
- [ ] 7. `test_graph_*`, `test_asof_join.py`
- **Gate:** 89 snapshots < ~10 min (else weekly + document); E3 ring recall clearly above E2

### Phase 5 — Isolation Forest, blend, explanations, artifacts (Day 8, ~8 h, 5/10)
- [ ] 1. `modeling/iforest.py` — fit on train only, label-free
- [ ] 2. `modeling/blend.py` — four `XGB_-k` models, grid over `w`, pick `w*` (§7.6) + fallback
- [ ] 3. `modeling/decisions.py` — REVIEW / HOLD thresholds on valid
- [ ] 4. `scoring/reasons.py` + `configs/reason_codes.yaml` — top-3 reasons + anomaly reason
- [ ] 5. `modeling/artifacts.py` — save/load `models/v1`, write `models/CURRENT`
- [ ] 6. `scoring/risk_model.py` — `RiskModel.load()` / `score_batch()` + startup compat checks
- [ ] 7. **E4**, **E5 (LOPO)** on valid, then `make evaluate-test V=v1` **exactly once** → `reports/results.md`
- [ ] 8. `test_scoring.py`, `test_reason_codes.py`, `test_training_smoke.py` (part 2)
- **🚩 CHECKPOINT (end of Day 8): `models/v1` + `reports/results.md` exist.** Exactly one line in `reports/test_runs.log`.

### Phase 6 — Streaming pipeline (Days 9-10, ~16 h, 7/10)
- [ ] 1. `features/store_redis.py` **first**, + `test_parity_redis.py` + `test_idempotency.py`
- [ ] 2. `stream/replayer.py` — pacing, label stripping, backpressure
- [ ] 3. `stream/sink.py` + `storage/parquet_io.py` + `storage/duck.py` — atomic writes, watermark, dedup view
- [ ] 4. `stream/scorer.py` — micro-batch loop, pending drain/reclaim, DLQ, ack-after-flush, HOLD, metrics, SIGTERM, `crash_after` hook
- [ ] 5. `stream/backfill.py` (§9.5)
- [ ] 6. `graph/refresh_live.py` (§6.5)
- [ ] 7. `scripts/rescore_check.py` (§9.6)
- [ ] 8. End-to-end run: full test window at 3,600x (replayer + scorer + graph-refresh)
- [ ] 9. `test_scorer_recovery.py`, `test_sink.py`, `test_replayer.py`, `test_graph_live.py`
- **🚩 CHECKPOINT (end of Day 10): test window replays end to end; re-score check 100% identical;** live snapshot == offline for 3 boundaries

### Phase 7 — FastAPI (Day 11 am, ~5 h, 3/10)
- [ ] 1. `schemas.py` models
- [ ] 2. `api/main.py` with lifespan (model loaded once, not per request)
- [ ] 3. Routes: `/health`, `/score`, `/alerts`, `/transactions/{id}`, `/metrics`
- [ ] 4. 503 handler
- [ ] 5. `test_api.py` incl. proof that `/score` never writes `state:acct:*`; 422/404/503 cases

### Phase 8 — Dashboard (Day 11 pm, ~4 h, 3/10)
- [ ] 1. `dashboard/app.py` — the six §11 panels
- [ ] 2. Label the replay scorecard as evaluation data (the scorer never sees labels)
- [ ] 3. Manual check during a live replay; works at 1280 px

### Phase 9 — Docker Compose and benchmark (Day 12, ~8 h, 5/10)
- [ ] 1. `Dockerfile` (§12.1) with exact uv + Redis tags pinned
- [ ] 2. Full `docker-compose.yml` (§12.2) + `.env.example` for Compose
- [ ] 3. `make up` / `demo` / `demo-reset` / `smoke`
- [ ] 4. `scripts/benchmark_stream.py` — capacity at batch {1,50,200,500}, latency at 50% and 80%
- [ ] 5. `make rescore-check` inside the Compose run
- [ ] 6. `reports/benchmark.md` — hardware, p50/p95/p99, staleness, `docker stats` peaks
- [ ] 7. CI: add `redis-integration` and `image-build` jobs (3 green jobs)

### Phase 10 — Documentation, demo, interview prep (Day 13 → Day 14 am, ~10 h, 3/10)
- [ ] 1. `README.md` in the §17 Phase-10 order (synthetic-data caveat on the first screen)
- [ ] 2. Demo recording (2-3 min) incl. `docker compose restart scorer` mid-replay showing no duplicates
- [ ] 3. CV bullets filled from `results.md` + `benchmark.md` (§20.3)
- [ ] 4. Interview prep — §21 questions, optional `docs/interview_notes.md`
- [ ] 5. **Clean-clone rerun**: fresh clone → `make setup all up demo` → numbers match `reports/`

### NICE TO HAVE — only after every MUST item is done, in this order (§2.2)
- [ ] Partitioned streams (K = 2-4) + scaling benchmark
- [ ] Ego-graph panel (`st.graphviz_chart`)
- [ ] Parquet compaction job
- [ ] Label-noise knob
- [ ] Relabel variant of the withheld-pattern experiment
- [ ] Strict snapshot alignment
- [ ] Slim serving image (`xgboost-cpu`)

---

## Decisions and deviations from PLAN.md

Record anything that departs from the plan, with the reason. Empty so far.

| Date | Deviation | Reason | Plan §|
|---|---|---|---|
| 2026-09-20 | Repo lives at `~/projects/...` not `~/code/...` | Existing layout on this VM | §15.2 |
| 2026-09-20 | Makefile targets for unbuilt phases `exit 1` instead of printing only | A target that claims to build data must not exit 0 without doing it | §16.1 |
| 2026-09-20 | Added `make help` (not in §16.1) and a placeholder `README.md` | `readme` is required by `pyproject.toml`, so the package cannot build without it; README is written properly in Phase 10 | §15.3, §17 |
| 2026-09-20 | Redis pinned to `redis:8.10.1-alpine`, not the plan's `redis:8-alpine` | §15.3 requires exact image tags; `8-alpine` resolved to server 8.10.1 (identical digest). CI must use this same tag | §12.2, §14, §15.3 |
| 2026-09-20 | Added `tests/test_redis_smoke.py` + a `redis_client` fixture in Phase 0, earlier than §13 schedules | `make test-redis` otherwise exits 5 with no tests collected, so the target was untestable. The fixture also guards DB 15 | §13 |
| 2026-09-20 | Makefile test targets now export `TEST_REDIS_URL` (DB 15) instead of inheriting `REDIS_URL` (DB 0) | The global `export REDIS_URL` leaked DB 0 into tests, which would have flushed live scorer state in Phase 6 | §13, §16.1 |

---

## How to update this file

After finishing a task:
1. Tick its box in *Remaining work*.
2. Add a row to *Completed* with the date and the evidence (file, report or command).
3. Rewrite *Currently working on* and *Next up*.
4. Update the counters at the top and the *Current phase* line.
5. At a phase end: confirm `make lint && make test` (+ `make test-redis` from Phase 6), commit, tag `phase-N`.
6. Log anything that diverged from `PLAN.md` in *Decisions and deviations*.
