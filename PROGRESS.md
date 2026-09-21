# PROGRESS — realtime-fraud-detection

Living status file. **Update it at the end of every completed task**, together with the commit for
that task. Source of truth for *what* to build is `PLAN.md`; this file only tracks *where we are*.

- **Plan version:** v1 (2026-09-16) · **Started:** 2026-09-20
- **Current phase:** Phase 6 — Streaming pipeline (PLAN §17, Phase 6) · Phases 0-5 complete · tags `phase-0`, `sim-v1`, `phase-2`, `sim-v2`, **`sim-v2-fix1`**
- **Phase 1 progress:** 8 / 8 ✅ — **simulator FROZEN, tagged `sim-v1`**
- **Phase 2 progress:** 7 / 7 ✅ — tagged `phase-2`
- **Phase 3 progress:** 6 / 6 ✅ (E3 ablation belongs to Phase 4)
- **Phase 4 progress:** 6 / 6 ✅
- **Phase 5 progress:** 8 / 8 ✅ — **COMPLETE**, test split evaluated once on 2026-09-21
- **Overall:** 43 / 73 must-have tasks done (+ 7 nice-to-have, not counted) · Phase 0: 7 / 7 ✅

---

## Currently working on

**Nothing — Phase 5 is closed. Phase 6 has not been started.**

The 🚩 Day-8 checkpoint is met: `models/v1/` exists, `reports/results.md` exists, and
`reports/test_runs.log` holds **exactly one line**.

Headline, from the single test evaluation (2026-09-21T01:59:13Z, commit `0d30e50`):
**PR-AUC 0.9938 on test against 0.9938 on validation** — the model did not overfit the
split it was tuned on.

Two things are reported rather than fixed, both in `reports/results.md`:

- **Test alert rate 2.09% exceeds the 2% review budget.** The threshold was frozen on
  validation and deliberately not re-tuned (§7.11). The cause is prevalence: the test
  window carries 2.01% fraud against validation's 1.34%, 1.51x as much, and at 99.3%
  recall alerts track the fraud count. E2 (2.08%) and E3 (2.10%) overshoot identically,
  so it is not specific to the blend.
- **HOLD stayed disabled.** It was switched off when thresholds were chosen on
  validation, before test was read, and the artifact was not rebuilt afterwards.

---

## Next up (in order)

1. **P6.1** — `features/store_redis.py` **first**: one pipeline for load, one `MULTI/EXEC` for commit, `ZADD`/`ZCOUNT` with an exclusive upper bound, periodic trimming, `decode_responses=True`. Plus `test_parity_redis.py` and `test_idempotency.py`.
2. **P6.2** — `stream/replayer.py`: pacing, label stripping, backpressure.
3. **P6.3** — `stream/scorer.py`: consumer group, idempotent per-event commit, `XACK` only after the sink flush.
4. **P6.4** — `stream/recovery.py`, `backfill.py`, and the §9.6 re-score check.

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
| 2026-09-20 | 0 | `.github/workflows/ci.yml` (`lint-unit` job) written; `origin` remote configured; GitHub SSH verified | CI steps pass locally |
| 2026-09-20 | 0 | Private repo created and `main` pushed | github.com/Ankit-c-lang/realtime-fraud-detection (PRIVATE) |
| 2026-09-20 | 0 | **CI green on GitHub** after fixing an unresolvable `setup-uv@v10` pin | run 35504020945, `lint-unit` ✓ in 20s |
| 2026-09-20 | 0 | **Phase 0 complete — tagged `phase-0`** | `git tag phase-0` |
| 2026-09-20 | 1 | `configs/splits.yaml` (§4.7), `categories.yaml` (10 fixed levels), `cities.csv` (15 IN + 8 intl), `sim.yaml` and `sim_tiny.yaml` | `tests/test_configs.py`, 24 tests |
| 2026-09-20 | 1 | `src/fraud/sim/population.py`: accounts, merchants, devices and the four IP pools, from one `SeedSequence` | `tests/test_sim_population.py`, 38 tests; full config builds in 1.8 s |
| 2026-09-20 | 1 | Cleanup: `merchants.online_merchant_share` (34.3% measured) and `ip_pools.office.usage_share` (0.50) as explicit knobs in both sim configs | `tests/test_configs.py` + population tests, 28 + 40 |
| 2026-09-20 | 1 | `src/fraud/sim/legit.py`: vectorised legitimate stream and all nine hard negatives | `tests/test_sim_hard_negatives.py`, 23 tests; full config 488,408 events in 35 s, -0.84% of target |
| 2026-09-20 | 1 | `src/fraud/sim/patterns.py`: all four injectors, ring mule accounts, deliberate ring placement | `tests/test_sim_patterns.py`, 24 tests; full config 8,362 fraud events in 2.4 s, prevalence 1.683%; every §4.5 quota and §4.8 signature check met |
| 2026-09-20 | 1 | RING target corrected 3000 -> 4250 in `sim.yaml` (approved); RING restored to the standard §4.8 band test | Now +0.8% of target; `test_ring_target_agrees_with_the_ring_structure` pins the two together |
| 2026-09-20 | 1 | `src/fraud/sim/generate.py` + `make data`: four Parquet tables and `manifest.json` | `tests/test_sim_schema.py` (16) and `tests/test_sim_determinism.py` (7); full run 494,156 events, 1.687% fraud, 14 MB, **79 s** |
| 2026-09-20 | 1 | `src/fraud/sim/checks.py`: all 26 §4.8 validations, exits non-zero on failure | `tests/test_sim_checks.py`, 11 tests, each group proven to fail on corrupted data |
| 2026-09-20 | 1 | `reports/sim_report.md` committed: **26/26 pass**, config sha256 `97404e57…` | `make data` |
| 2026-09-20 | 2 | P2.1 (tests first): `test_features_windows.py` + `test_features_geo.py`, 46 hand-computed tests written before any implementation | commit `fed3e52` |
| 2026-09-20 | 2 | P2.2: `schemas.py`, `features/spec.py` (30 hot + 6 warm, `fs1`), `state.py`, `accounts.py`, `engine.py`, `store_memory.py`, `configs/features.yaml` | **all 46 P2.1 tests pass, no expected value changed**; 203 fast / 211 full |
| 2026-09-20 | 2 | P2.3: `features/replay.py` + `make features`; `hot_features.parquet` (494,156 rows, 0 NaN) and `checkpoint_2026-03-14.json.gz` (21,552 accounts, 4.6 MB) | `tests/test_replay.py` (14) + `test_state_serialization.py` (10); replay **81.5 s**, 6,060 events/s |
| 2026-09-20 | 2 | `reports/feature_report.md` committed | `make features` |
| 2026-09-20 | 2 | **Phase 2 complete — tagged `phase-2`** | 227 fast / 235 full tests green |
| 2026-09-20 | 3 | `modeling/splits.py` (the only split loader, `ALLOW_TEST` lock) + `configs/model.yaml` | `tests/test_splits.py`, 14 tests; train 213,470 / early_stop 38,580 / valid 66,558 rows |
| 2026-09-20 | 3 | `modeling/metrics.py`: PR-AUC, budget-constrained operating point, HOLD threshold, per-pattern recall, value detection rate | `tests/test_metrics.py`, 19 tests |
| 2026-09-20 | 3 | `modeling/rules.py` + `experiments.py`; **E1 on valid**: PR-AUC 0.6305, P 0.928, R 0.673, alerts 1.04%, VDR 0.272 | `reports/experiments/E1.json`; `tests/test_rules.py`, 18 tests |
| 2026-09-20 | 3 | `modeling/train_xgb.py` + `make experiments`; **E2 on valid**: PR-AUC 0.9995, P 0.953, R 0.996, alerts 1.50%, VDR 0.997, every pattern ~1.00 | `reports/experiments/E2.json`, `xgb_search.json`; 20 trials in 94 s |
| 2026-09-20 | 3 | `tests/test_training_smoke.py`, 9 slow tests (category levels from config, no reweighting, search reproducible) | `make test-all` |
| 2026-09-20 | 3 | **E2 beats E1 by +0.3689 PR-AUC — the Day-5 checkpoint is met** | `reports/experiments/` |
| 2026-09-20 | 3 | ⚠️ **E2 crosses the §4.8 threshold (>0.995); `sim-v2` decision pending** | `reports/sim_realism_review.md` |
| 2026-09-20 | 3 | **`sim-v2` applied and frozen** (PLAN §4.8 one-time revision): widely shared devices, travel/VPN on a new device, tighter sprees. 26/26 checks pass; 494,189 events, 1.691% fraud | `reports/sim_realism_review.md`; sha256 `4013268a…` |
| 2026-09-20 | 3 | Re-ran E1/E2 on valid only. **E1 0.6305 -> 0.4181** (precision 0.928 -> 0.622, FPR 6.75x) — the negatives genuinely overlap now. **E2 0.9995 -> 0.9973**, ring recall still 1.00 | `reports/experiments/` |
| 2026-09-20 | 4 | `scripts/entity_fanout_stats.py` + `reports/entity_fanout.md`: fan-out over 39 daily 30-day train windows, every entity type labelled | 849,923 device / 699,838 IP (entity, window) pairs |
| 2026-09-20 | 4 | **Bug fix `sim-v2-fix1`**: `next_device()` now registers what it allocates. Spares were unregistered, so attacker devices reused their ids | 0 rows were affected (proved); E1/E2 identical after regeneration; 2 regression tests |
| 2026-09-20 | 4 | `configs/graph.yaml`: **`device_cap: 16`, `ip_cap: 18`** chosen against the train-only fan-out distributions | `reports/entity_fanout.md` |
| 2026-09-20 | 4 | `graph/projection.py` (DuckDB, sorted edges) and `graph/algorithms.py` (degree, clustering, seeded Louvain, community aggregates, personalised PageRank) | `test_graph_projection.py` (13), `test_graph_algorithms.py` (16), `test_graph_leakage.py` (13) |
| 2026-09-20 | 4 | Real snapshot at 2026-02-20: 2,614 nodes, 6,143 edges, 642 communities, 320 seeds in 1.6 s. **Clustering mean 0.794, non-zero on 82%** | confirms the §1 bipartite correction |
| 2026-09-20 | 4 | `graph/snapshots.py` + `graph/join.py` + `make graph`: **89 snapshots in 104.5 s** (slowest 1.34 s), 533-2,653 nodes, 0-370 seeds | `data/graph/offline/`, `calendar.parquet` |
| 2026-09-20 | 4 | `training_table.parquet`: 494,189 rows, all 36 features, 0 NaN, 12.6% with a non-default graph position | `tests/test_asof_join.py`, 12 tests |
| 2026-09-20 | 4 | **E3** (36 features, E2's params unchanged): PR-AUC 0.9973, ring recall 1.00 — **identical to E2 to 4 dp**, no measurable gain | `reports/experiments/E3.json` |
| 2026-09-20 | 4 | **E3b** (6 graph features only, behaviour ablated): ring recall **1.00 at precision 1.000**, and **0.00 on every other pattern** | `reports/experiments/E3b.json` |
| 2026-09-20 | 4 | Graph features take **50.1% of total importance**; `community_shared_devices` ranks **#1 of 36** | `reports/graph_ablation.md` |
| 2026-09-20 | 4 | **Phase 4 complete** | 335 tests green |
| 2026-09-20 | 1 | **SIMULATOR FROZEN — tagged `sim-v1`.** `configs/sim.yaml` sha256 `97404e57e5da143b1f1b47c11caac96d315ce5fe85da1f47feba5cda9976863f` matches the report and the run manifest | `git tag sim-v1`; CLAUDE.md invariant 11 |
| 2026-09-20 | 5 | `modeling/iforest.py` — label-free Isolation Forest on all 213,215 train rows, 35 numeric features, 1,001-quantile percentile map, per-feature medians and spreads | `tests/test_iforest.py` 13 passed |
| 2026-09-20 | 5 | `modeling/blend.py` + `make lopo` — four `XGB_-k` models, grid over `w`, `w*` by leave-one-pattern-out; **`w* = 0.5`**, so the Isolation Forest earns real weight and the §7.6 fallback is not needed | `reports/experiments/lopo.json`, `tests/test_blend.py` 17 passed |
| 2026-09-20 | 5 | `modeling/decisions.py` — REVIEW threshold inside the 2% budget, HOLD threshold at the 0.95 precision bar, `decide()` → ALLOW/REVIEW/HOLD | `tests/test_scoring.py` |
| 2026-09-20 | 5 | `configs/reason_codes.yaml` (all 36 features) + `scoring/reasons.py` — native `pred_contribs`, top-3 positive contributions, anomaly reason | additivity 2.0e-05 on 5,000 valid rows, `tests/test_reason_codes.py` 16 passed |
| 2026-09-20 | 5 | `modeling/artifacts.py` + `make train V=v1` — **`models/v1` and `models/CURRENT` written** with the complete §8 metadata | `models/v1/` 6 files; 🚩 Day-8 checkpoint half met |
| 2026-09-20 | 5 | `scoring/risk_model.py` — the one path from features to decisions, with startup refusals on feature-spec and library drift | `tests/test_scoring.py` 31 passed |
| 2026-09-20 | 5 | **E4** (blend at `w*`, scored through `RiskModel`) and **E5** (LOPO evidence) on valid | `reports/experiments/E4.json`, `E5.json` |
| 2026-09-20 | 5 | `modeling/evaluate_test.py` — `ALLOW_TEST=1` guard, threshold taken from the artifact, `metrics.test` written once, append-only `reports/test_runs.log` | `tests/test_evaluate_test.py` 17 passed; **not run against test** |
| 2026-09-20 | 5 | `scripts/export_results.py` + `make results` — generates `reports/results.md` (§7.9 tables) and the shap beeswarm | `reports/results.md`, `reports/figures/global_contributions.png` |
| 2026-09-21 | 5 | **Test split evaluated once** (user-run). Test PR-AUC **0.9938** vs valid 0.9938; recall 0.993, precision 0.958, VDR 0.996; RING 0.99, ATO/CT 1.00, VEL 0.97 | `reports/test_runs.log` (1 line), `models/v1/metadata.json` `metrics.test`, `reports/experiments/E{1,2,3,4}_test.json` |
| 2026-09-21 | 5 | Report generator now states the **2.09% test alert rate vs the 2% budget** with its cause, the test-run provenance line, and that **HOLD was disabled before test and unchanged** | `reports/results.md`; `tests/test_evaluate_test.py` 24 passed |
| 2026-09-21 | 5 | **Phase 5 closed.** 🚩 Day-8 checkpoint met | 440 tests green |

---

## Remaining work

Checkpoints are hard gates (PLAN §2.4). If one is missed, apply the cut list in §2.4 **in order**
before continuing — do not silently slip.

### Phase 0 — Environment and skeleton (Day 1 am, ~4 h, 2/10) — ✅ COMPLETE (2026-09-20)
- [x] 1. Verify environment and VM allocation (§15.1)
- [x] 7a. `CLAUDE.md` from §18.2
- [x] 2. `git init` + private GitHub repo + push; `PLAN.md` committed
- [x] 3. Skeleton from §16: empty modules w/ docstrings, `pyproject.toml` (§15.3), `.python-version`, Makefile stubs, `.gitignore`, `.dockerignore`, `.env.example`, `src/fraud/config.py`
- [x] 4. `uv lock && uv sync --all-groups`
- [x] 5. `docker-compose.yml` with the `redis` service only; `make redis-up` → `PONG`
- [x] 6. `.github/workflows/ci.yml` (`lint-unit`) + `tests/test_smoke.py` — green on GitHub
- [x] 7b. Commit + push; `make setup && make lint && make test` green; tag `phase-0`

### Phase 1 — Simulator (Day 1 pm → Day 3 midday, ~16 h, 5/10) — ✅ COMPLETE (2026-09-20), tagged `sim-v1`
- [x] 1. Configs: `sim.yaml`, `sim_tiny.yaml`, `cities.csv` (15 IN + 8 intl), `categories.yaml`, `splits.yaml`
- [x] 2. `sim/population.py` — accounts, merchants, devices, IP pools
- [x] 3. `sim/legit.py` — legitimate behaviour + all hard negatives (§4.4)
- [x] 4. `sim/patterns.py` — 4 fraud injectors + ring constraints (§4.5)
- [x] 5. `sim/generate.py` — assemble, stable sort, `txn_id`, Parquet + `manifest.json`
- [x] 6. `sim/checks.py` — §4.8 checks → `reports/sim_report.md`
- [x] 7. `test_sim_*` tests
- [x] 8. **Freeze** `sim.yaml` (hash in report), commit, tag `sim-v1`
- **Gate:** ~500K events, 1.2-1.8% fraud, ≥12 rings start in test window (≥4 reusing a device), same seed → same hashes

### Phase 2 — Feature engine and offline replay (Day 3 pm → Day 5 am, ~14 h, 6/10) — ✅ COMPLETE (2026-09-20), tagged `phase-2`
- [x] 1. `features/spec.py` — `FEATURE_NAMES`, defaults, `FEATURE_SPEC_VERSION = "fs1"`
- [x] 2. `features/state.py` — `AccountState` with exact JSON round trip
- [x] 3. `features/accounts.py` — `AccountDirectory`
- [x] 4. `features/engine.py` — `compute_features()`, `update_account()`, `FeatureEngine.process()`
- [x] 5. `features/store_memory.py` — blobs + windowed entity indexes
- [x] 6. `features/replay.py` — `hot_features.parquet` + checkpoint at `test_start`
- [x] 7. `test_features_*`, `test_state_serialization.py`, `test_replay.py` (hand-computed values)
- **Gate:** `make features` < ~3 min, no NaNs, row count == event count, checkpoint round-trips

### Phase 3 — Splits, baselines, XGBoost (Day 5 → Day 6 am, ~8 h, 4/10) — ✅ COMPLETE (2026-09-20) · 🚩 checkpoint met: E2 beats E1
- [x] 1. `modeling/splits.py` (burn-in excluded, test behind `ALLOW_TEST`)
- [x] 2. `modeling/metrics.py` (PR-AUC, capacity operating point, per-pattern recall, VDR, FPR)
- [x] 3. `modeling/rules.py` — R1-R4 → **E1**
- [x] 4. `modeling/train_xgb.py` — 20-config random search on hot features → **E2**, save param set P
- [x] 5. `modeling/experiments.py` — writes `reports/experiments/E*.json`
- [x] 6. `test_splits.py`, `test_metrics.py`, `test_rules.py`, `test_training_smoke.py` (part 1)
- **🚩 CHECKPOINT (end of Day 5): E2 on valid exists.** E2 must beat E1 on PR-AUC and recall@budget.

### Phase 4 — Graph layer (Day 6 pm → Day 7, ~14 h, 6/10) — ✅ COMPLETE (2026-09-20)
- [x] 1. Measure accounts-per-device / per-IP on train; set `device_cap`, `ip_cap` in `graph.yaml`
- [x] 2. `graph/projection.py` (§6.1 SQL, with `ORDER BY`)
- [x] 3. `graph/algorithms.py` — degree, clustering, seeded Louvain, community aggregates, personalized PageRank w/ label delay
- [x] 4. `graph/snapshots.py` — daily offline snapshots + `calendar.parquet`
- [x] 5. `graph/join.py` — two-step point-in-time join → `training_table.parquet`
- [x] 6. **E3** (36 features, param set P) + ablation vs E2
- [x] 7. `test_graph_*`, `test_asof_join.py`
- **Gate:** 89 snapshots < ~10 min (else weekly + document); E3 ring recall clearly above E2

### Phase 5 — Isolation Forest, blend, explanations, artifacts (Day 8, ~8 h, 5/10) — ✅ COMPLETE (2026-09-21)
- [x] 1. `modeling/iforest.py` — fit on train only, label-free ✅ 2026-09-20
- [x] 2. `modeling/blend.py` — four `XGB_-k` models, grid over `w`, pick `w*` (§7.6); `w* = 0.5`, fallback not needed ✅ 2026-09-20
- [x] 3. `modeling/decisions.py` — REVIEW / HOLD thresholds on valid; **HOLD disabled, it does not separate** ✅ 2026-09-20
- [x] 4. `scoring/reasons.py` + `configs/reason_codes.yaml` — top-3 reasons + anomaly reason, all 36 features ✅ 2026-09-20
- [x] 5. `modeling/artifacts.py` — save/load `models/v1`, write `models/CURRENT`, `make train V=v1` ✅ 2026-09-20
- [x] 6. `scoring/risk_model.py` — `RiskModel.load()` / `score_batch()` + startup compat checks ✅ 2026-09-20
- [x] 7. **E4**, **E5 (LOPO)** on valid ✅ 2026-09-20 · **`make evaluate-test V=v1` run by the user once on 2026-09-21** → `reports/results.md`, one line in `reports/test_runs.log` ✅
- [x] 8. `test_scoring.py` (31) + `test_reason_codes.py` (16) ✅ 2026-09-20 · `test_training_smoke.py` part 2 still open
- **🚩 CHECKPOINT (end of Day 8): MET ✅ 2026-09-21** — `models/v1/` and `reports/results.md` exist; `reports/test_runs.log` has exactly one line.

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

Record anything that departs from the plan, with the reason.

### ✅ RESOLVED — RING volume target (approved 2026-09-20)

PLAN §4.5 gave the ring pattern **both** a structure and a total, and they disagreed:

- structure: ~45 rings x 6-15 accounts x 6-12 transactions each -> midpoints give **~4,250**
- stated total: **"~3,000 transactions"**

`patterns.ring.target_transactions` in `sim.yaml` is corrected **3000 -> 4250**. This is a
correction to an internally inconsistent estimate in the plan, **not** tuning of the model
or of any result: no structural range, ring count, quota or generated value was touched,
and the generator produces exactly what it produced before the change (4,282 events, now
+0.8% of target instead of +42.7%). RING is back under the ordinary §4.8 ±30% band test.

`sim_tiny.yaml` was left at **200**, not raised to 250. Its ring ranges are its own
(8 rings x 4-6 accounts x 4-6 transactions), whose midpoints give exactly 200, so the file
is already self-consistent; raising it would have newly introduced the very inconsistency
being fixed and pushed the tiny run to -33.6%, outside the band. A test now pins the
configured ring target to what its own structure implies, so the two cannot drift apart
again in either file.

| Date | Deviation | Reason | Plan §|
|---|---|---|---|
| 2026-09-20 | Repo lives at `~/projects/...` not `~/code/...` | Existing layout on this VM | §15.2 |
| 2026-09-20 | Makefile targets for unbuilt phases `exit 1` instead of printing only | A target that claims to build data must not exit 0 without doing it | §16.1 |
| 2026-09-20 | Added `make help` (not in §16.1) and a placeholder `README.md` | `readme` is required by `pyproject.toml`, so the package cannot build without it; README is written properly in Phase 10 | §15.3, §17 |
| 2026-09-20 | Redis pinned to `redis:8.10.1-alpine`, not the plan's `redis:8-alpine` | §15.3 requires exact image tags; `8-alpine` resolved to server 8.10.1 (identical digest). CI must use this same tag | §12.2, §14, §15.3 |
| 2026-09-20 | Added `tests/test_redis_smoke.py` + a `redis_client` fixture in Phase 0, earlier than §13 schedules | `make test-redis` otherwise exits 5 with no tests collected, so the target was untestable. The fixture also guards DB 15 | §13 |
| 2026-09-20 | Makefile test targets now export `TEST_REDIS_URL` (DB 15) instead of inheriting `REDIS_URL` (DB 0) | The global `export REDIS_URL` leaked DB 0 into tests, which would have flushed live scorer state in Phase 6 | §13, §16.1 |
| 2026-09-20 | CI pins `actions/checkout@v7` and `astral-sh/setup-uv@v10`, not the plan's v5/v6 | §14 says to use the current major version; v7 and v10 are current as of today | §14 |
| 2026-09-20 | CI runs `make lint` / `make test` instead of the raw `uv run ruff`/`pytest` lines | Keeps the CI gates identical to the local ones so they cannot drift. The `uv sync --frozen` step is kept exactly as the plan has it | §14 |
| 2026-09-20 | `setup-uv` pinned to the exact `v10.1.0`, not a floating major | The project publishes floating major tags only through v7; `@v10` does not resolve and failed the first CI run | §14, §15.3 |
| 2026-09-20 | `sim_tiny.yaml` keeps the full 90-day calendar, shrinking only population and attack volumes | Lets `configs/splits.yaml` apply unchanged so every structural check runs against the tiny config too. **RE-MEASURED after `generate.py` (2026-09-20): a complete tiny run is 1.40-1.42 s over three runs, and the two Parquet-level test files together take 8.5 s. §13's "finishes in seconds" intent is satisfied, so the 90-day calendar STAYS.** §13 also describes the fixture as "300 accounts, 6 days"; ours is 1,200 accounts over 90 days, which is the deviation, and it buys real split coverage. Watch `make test` (now 15 s): if it passes ~30 s, revisit | §4.8, §13 |
| 2026-09-20 | `sim_tiny.yaml` ring thresholds are far lower than §4.5 (1 in test vs 12, 3 in train vs 20) | The §4.5 constraints need ~40 rings; 40 rings in a 20K-event set would push prevalence well past 2%. Thresholds live in the config so one check implementation serves both files. **The full `sim.yaml` keeps the real §4.5 values** | §4.5, §4.8 |
| 2026-09-20 | ~~`is_online` derived as `online_share >= 0.95` (40%)~~ **RESOLVED**: new `merchants.online_merchant_share: 0.35` knob in both sim configs; `is_online` is now sampled from it and is independent of `online_share` | Merchant reference data (§4.2) and transaction channel are separate concerns. §5.2 feature 4 derives the model's `is_online` from the **event channel**, not this column, so the two never needed coupling. Measured: 34.3% | §4.3, §4.2, §5.2 |
| 2026-09-20 | New `ip_pools.office.usage_share: 0.50` in both sim configs | **Simulator design choice, not specified by PLAN.md.** Office IP use was effectively certain during weekday working hours, which made affiliated accounts look like they lived at the office. 0.50 splits their weekday-daytime online activity with the home IP. `population.py` carries the value; the weekday and 09:00-18:00 gate belongs to `legit.py`. Named `usage_share` inside the `office:` block rather than `office_usage_share`, to match the sibling `carrier_nat` and `vpn` keys so one code path reads all three | §4.3 |
| 2026-09-20 | Ring mule accounts are created by `patterns.py`, not `population.py` | Their ages are defined relative to each ring's start date (§4.5), which `population.py` cannot know. It builds the 22,000 legitimate accounts only | §4.3, §4.5 |
| 2026-09-20 | Shared IPs (NAT, office) fall back to the **nearest** covered city, never a random one | A pool with fewer IPs than cities cannot cover every city. A random fallback fabricates impossible travel and would poison `geo_speed_kmh` for legitimate accounts. Raised full-config same-city placement from 93.3% to 95.7% | §4.3, §5.2 |
| 2026-09-20 | Regular merchants are drawn from the account's **reachable** pool (online, or physical in its home city), not globally by popularity | A global draw gave a Mumbai account a regular shop in Delhi, so every routine visit looked like travel and `geo_speed_kmh` became meaningless. Defect found while writing `legit.py`; 0 unreachable physical regulars now | §4.4, §5.2 |
| 2026-09-20 | Channel is decided by category `online_share` alone; POS events are then grounded onto a merchant with a storefront in the account's current city | Letting `is_online` force the channel pushed ONLINE to 92.6%. The grounding step keeps the category (so amounts and the channel mix are unchanged) and guarantees a card-present event is local. Measured 58.5% ONLINE on tiny vs 56.6% category-implied; 0 POS at storefront-less merchants | §4.4, §5.2 |
| 2026-09-20 | The activity rescale subtracts fraud **and** the expected shopping-spree and micro-burst volume | Scaling the base draw to the full target overshot by +13.8%, outside the ±5% tolerance, because the hard negatives are injected on top. Now -0.84% | §4.3 |
| 2026-09-20 | Travel is applied **after** sprees and micro-bursts are injected | Applying it first left a spree seeded at home sitting inside the trip window in the wrong city, producing a 0.01 h city change no journey could explain. Minimum POS city-change gap is now 15.95 h | §4.4 |
| 2026-09-20 | Injected spree and burst rows inherit their seed's IP, so a few card-present rows carry a shared office IP | Keeping the burst on one connection is the point of the hard negative. Harmless: a POS location comes from the merchant, and the account genuinely belongs to that office cluster | §4.4 |
| 2026-09-20 | Ring placement is assigned deliberately (20 to train, 12 to test, rest anywhere) rather than drawn at random | The §4.5 quotas fail by chance otherwise, and the ring half of the evaluation would then measure nothing. `checks.py` re-verifies the outcome rather than trusting the assignment | §4.5 |
| 2026-09-20 | A domestic ATO picks an Indian city **>900 km** from the victim's last purchase | §4.8 requires >900 km/h at the first fraud event and the lag is at most an hour, so a neighbouring city would not register as a jump at all. Now 100% of 120 attacks clear the bar | §4.5, §4.8 |
| 2026-09-20 | `patterns.py` reads `configs/splits.yaml` directly | The ring quotas are defined against the train and test windows, so the simulator has to know where they are. It reads the single source of truth; `modeling/splits.py` remains the only loader of split *rows* | §4.5, §4.7, invariant 6 |
| 2026-09-20 | Card-testing victims are drawn only from accounts that exist at the attack time | Drawing from every account let an attack probe a card created weeks later, putting events before their own account and breaking a §4.8 check | §4.5, §4.8 |
| 2026-09-20 | `generate.py` pins timestamps to `datetime64[us]` and text columns to the nullable `string` dtype | The generators emit a mix of second and nanosecond resolution and Parquet stores neither unchanged, so a written table did not compare equal to the one in memory | §4.2 |
| 2026-09-20 | Injected sprees and micro-bursts are grounded on the **account's home city**, not the seed event's city | A seed sent over a carrier-NAT IP in another city dragged the spree's card-present rows to that city, faking a location jump minutes later. Found when the travel-gap test caught a 54-minute Mumbai-to-Delhi hop | §4.4, §5.2 |
| 2026-09-20 | Travel gap enforcement walks in time order against the last **kept** event | Checking only the single boundary-crossing event was not enough: once it was dropped, the next event on the far side inherited the too-short gap and survived | §4.4 |
| 2026-09-20 | `configs/features.yaml` created (windows, thresholds, caps) | Listed in §16 but never written out. It describes how features are derived from the frozen data, not the data itself, so it is **not** covered by the sim-v1 freeze. Changing a value in it means bumping `FEATURE_SPEC_VERSION` | §5.1, §16 |
| 2026-09-20 | `TransactionEvent` is a frozen dataclass, not a Pydantic model | The replay builds one per event across ~494k events, where validation costs more than it buys on data from our own generator. The API validates with Pydantic before constructing one (§10) | §3.5, §10 |
| 2026-09-20 | Timestamps convert via a fixed naive epoch, never `datetime.timestamp()` | `.timestamp()` reads a naive value in the machine's local zone, so identical input would give different state on another machine and the replay would stop being reproducible | §5.1 |
| 2026-09-20 | Test helpers live in `tests/feature_helpers.py`, imported directly rather than as `tests.*` | `tests/` is not a package, and putting the builders in `conftest.py` would have broken collection for the whole suite while the feature modules did not exist | §13 |
| 2026-09-20 | The replay peaks at **2.1 GB RSS** on the full dataset | It accumulates one dict per event before building the frame. Comfortable on a 7.7 GB VM but the largest memory user so far. If Phase 4's graph work pushes the total, stream the output in batches instead | §5.5 |
| 2026-09-20 | The split guard is `allow_test_enabled()` and the exception is `SplitLockedError`, not `test_*` / `Test*` | pytest collects anything so named as a test case or class and warns. A guard that silently becomes a test is a guard nobody is checking | §7.11 |
| 2026-09-20 | `configs/model.yaml` created (alert budget, HOLD bar, rule thresholds) | Listed in §16; holds the §7.7 decision-policy values. Like `features.yaml` it governs modelling rather than the data, so it sits outside the sim-v1 freeze | §7.3, §7.7, §16 |
| 2026-09-20 | `configs/model.yaml` rules corrected to match §7.3 exactly | The first draft had R2-R4 wrong (it merged the ATO and fan-out rules and dropped the ring rule). §7.3 is: R1 `acct_cnt_5m>=5`; R2 `geo_speed_kmh>=900 AND new_device`; R3 `dev_accts_1h>=5 OR (acct_small_1h>=3 AND acct_declines_1h>=2)`; R4 `dev_accts_30d>=3 AND account_age_days<30` | §7.3 |
| 2026-09-20 | E1 is reported where the rules fire, not at a budget-constrained threshold | Rules have no threshold to turn down at serving time. Forcing them through the operating-point search would hide how much they over-alert, which is half of what the baseline is for. `metrics.evaluate_at` was added for this | §7.3, §7.7 |
| 2026-09-20 | `splits.load` now also joins the raw `amount` from `events.parquet` | Value detection rate weights recall by money and the feature table only carries `log_amount`. `amount` travels with the row but is **not** a feature: `feature_matrix` still selects by the spec | §7.8 |
| 2026-09-20 | **`sim-v2`: the one revision §4.8 allows, now spent.** Four config knobs — `devices.shared_device_share` (5-15 account groups), `travel.new_device_share`, `vpn.new_device_share`, `shopping_spree.tight_share` | E2 hit 0.9995 with literally zero legitimate overlap on three fraud shapes. After: 866 legit rows at `dev_accts_30d>=5` (was 0), 283 at `acct_cnt_5m>=5` (was 1). Legit `dev_accts_30d` now reaches 14 against a ring median of 4. **No sim-v3** | §4.8 |
| 2026-09-20 | Two hard-negative tests rewritten for the new intent | They asserted the old too-clean behaviour: that every VPN session used an owned device, and that every shared device was a household one. Both are deliberately false under sim-v2. The replacements assert the *new* contract, including a new test that a legitimate device must reach the ring band | §4.4, §4.8 |
| 2026-09-20 | `sim-v2-fix1`: device-id registration bug fixed and everything regenerated | `next_device()` allocated an id but left registration to the caller, and the spare-handset code forgot, so `patterns.py` numbered attacker devices on top of spare ids. **Correctness only — no knob or realism assumption changed**, `sim.yaml` is byte-identical, so the §4.8 revision is not re-spent. Committed data was unaffected by luck (0 ids carried both legit and fraud events); a different seed would have fused a traveller's phone with an attacker's. Confirmed harmless by regenerating: same 494,189 events, same 1.691% fraud, E1 0.4181 and E2 0.9973 unchanged to 4 dp | §6.1 |
| 2026-09-20 | Caps set to `device_cap: 16`, `ip_cap: 18` — §6.1's own placeholders, kept after reviewing the measured distributions | §6.1 also suggests the 99.5th percentile, which on this data is **4** for devices. That would erase the rings the graph exists to find, since a ring puts 6-15 accounts on a device. 16 sits above rings (max 15) and sim-v2 shared devices (max 14) and below card-testing devices (p90 42). 18 keeps ring IPs (max 14) and drops carrier NAT (median 114), VPN (max 58) and offices (median 27) — §6.1 names big offices as something the cap *should* drop, since an office IP fuses colleagues into a ring-shaped cluster | §6.1 |
| 2026-09-20 | `ppr_risk` is ~1e-5 rather than exactly 0 for accounts the seeds cannot reach | Power iteration starts from a uniform vector and stops at the default tolerance. Five orders of magnitude below seeded values, so it cannot move a tree split; forcing it to zero would cost iterations for nothing. Documented in `algorithms.py` and asserted as a ratio, not as zero | §6.3 |
| 2026-09-20 | **The graph ablation shows no recall gain, and that is the reported result** | E2 already reaches ring recall 1.00, so E3 has no headroom. E3 matches E2 to 4 dp. The ablation instead reports two things it *can* establish: the graph features take 50.1% of importance with `community_shared_devices` ranked #1 of 36, and alone (E3b) they catch 100% of rings at 100% precision and 0% of every other pattern. The README will say the features are redundant on this dataset, not useless, and will not claim a delta the evidence does not support | §7.9 |
| 2026-09-20 | **`w* = 0.5` sits at the edge of §7.6's grid and wins by ~2 transactions** | Mean LOPO recall: w=0.5 → 0.85553, w=0.7 → 0.85497, w=0.9 → 0.84424, w=1.0 → 0.73956. The curve is a noisy plateau from 0.9 down, and the margin between the top two is 0.00056 — about two rows across the four runs. `w*` is taken as the plan specifies (argmax, ties to the larger `w`); the grid was **not** extended past 0.5 to look for a better point, because that is tuning the plan rather than following it. The flatness is the honest caveat and belongs in the README | §7.6 |
| 2026-09-20 | Anomaly-reason spread falls back to the standard deviation where the IQR is zero | §7.5 says "a small floor on the IQR". Most of these features are counts that are 0 for well over half of train, so their IQR is exactly 0 and a literal floor would make `\|x − median\| / IQR` enormous for any non-zero value — the sparsest feature would win every anomaly reason it appeared in. A zero IQR means the spread lives in the tails, so the standard deviation is used there; a genuinely constant column keeps a 1e-12 floor, where the numerator is 0 anyway | §7.5 |
| 2026-09-20 | Isolation Forest and blend hyperparameters moved into `configs/model.yaml` (`iforest:`, `blend:`) | §7.5 and §7.6 give them as literals, but CLAUDE.md forbids hard-coded thresholds and §8 has to record them in the artifact metadata. Values are exactly the plan's | §7.5, §7.6 |
| 2026-09-20 | **The HOLD tier is DISABLED: it does not separate from REVIEW on this data** | §7.7 picks `t_h` as the lowest threshold with precision ≥ 0.95 over ≥ 50 alerts, which assumes precision at the budget threshold is *below* the bar. Here it is **0.961**, so the whole alert set qualifies and `t_h == t_r` exactly: every one of the 909 alerts would become a HOLD, freezing 35 genuine customers and leaving the analyst queue empty. That is not a two-tier policy, so it is treated as §7.7's existing "nothing qualifies" case — HOLD off, the measured precision kept in metadata, and the README must say the tiers did not separate. The 0.95 bar was **not** raised to manufacture a split | §7.7 |
| 2026-09-20 | Anomaly-reason labels are phrases, not column names, and `log_amount` renders in rupees | An alert reading "log_amount 7.82" is not an explanation. `reason_codes.yaml` carries a `label` (for "Unusual combination: X and Y") and a `template` per feature, plus one presentation-only `expm1` transform. The transform never touches a model input | §7.10 |
| 2026-09-20 | `p_xgb` is computed as the logistic of the margin rather than by a second `predict_proba` call | The reasons come from `pred_contribs`, which sums to that same margin. Deriving both from one number makes the score and its explanation provably consistent, and the §9.6 re-score check compares to 1e-9 | §7.10, §9.6 |
| 2026-09-20 | `make train` reads `w*` from `reports/experiments/lopo.json` instead of recomputing it | The weight in the artifact is then traceable to a recorded experiment rather than to a fit that happened during packaging, and `make train` stays an 8-second step. It fails with a clear message if the file is missing | §7.6, §0.4 |
| 2026-09-20 | Only alert rows are explained | `pred_contribs` is the expensive part of scoring and §7.10 only ever shows reasons for REVIEW and HOLD rows. ALLOW rows get an empty list | §7.10 |
| 2026-09-20 | `build()` computes `p_xgb` from the booster margin, and the saved artifact is verified to reproduce its own scores | The first build recorded valid metrics from `predict_proba` while `RiskModel` scores from the margin. They agree to ~1e-7, which was enough to move 6 rows across the threshold and leave the metadata claiming PR-AUC 0.9940 where the deployed artifact produced 0.9938. Both paths now use the margin and `_verify_round_trip` asserts max \|risk diff\| ≤ 1e-9 at build time; it currently measures exactly 0 | §8, §9.6 |
| 2026-09-20 | `evaluate_test.py` applies thresholds chosen on valid, and refits E2/E3 deterministically rather than reusing stored models | Re-deriving a threshold on test would tune the decision to the data judging it (L10). E2 and E3 have no saved artifact, so they are refit from `train` with the recorded seeded parameters — identical models, nothing fitted on test. E4 comes straight from `models/v1` | §7.11 |
| 2026-09-20 | `EvaluationRun`, not `TestRun` | pytest collects any class named `Test*` and warns — the same trap that renamed `SplitLockedError`. Second occurrence, so it is worth stating as a convention | §13 |
| 2026-09-20 | The global contribution figure is a real `shap` beeswarm, not the matplotlib fallback | §7.10 allows either. `shap` 0.52 and XGBoost 3.4 agreed, and the beeswarm shows direction and spread rather than magnitude alone — low `account_age_days` and high `dev_accts_30d` visibly push towards fraud. The fallback path is kept and still reachable | §7.10 |
| 2026-09-21 | **ACCEPTED by the user: `v1` ships with HOLD disabled — all 909 flagged transactions go to REVIEW at 0.960 precision** | Confirmed on 2026-09-21 after the §7.7 tier collapse was reported. `ALLOW`/`REVIEW`/`HOLD` stays the decision vocabulary, so a later version can enable the tier with no schema change, and the scorer's `hold:acct:{id}` path (§7.7, Phase 6) is simply dormant in `v1`. Verified end to end: `thresholds.hold = null`, `hold_enabled = false`, `hold_degenerate = true`; `metrics.valid.hold` reports `enabled: false, source: "policy"`; `E4.json` matches byte for byte; `results.md` states the policy; scoring the valid split through `RiskModel` yields 65,440 ALLOW + 909 REVIEW and **zero HOLD** | §7.7 |
| 2026-09-21 | The reported HOLD block now follows the policy instead of re-searching, and `metrics.hold` gained `enabled` and `source` | Verifying the accepted decision found a real inconsistency: `evaluate_at` ran its own `hold_threshold()` search regardless of the policy, so `models/v1/metadata.json` and `E4.json` advertised a live tier at threshold `0.5128` beside `thresholds.hold = null` — telling a reader the system freezes cards when it does not. `evaluate_at(..., hold=...)` now takes the policy; left unset it still searches, which is the right question for a candidate model nobody ships (`E2`/`E3`/`E3b` keep `source: "search"`). `models/v1` was rebuilt to regenerate the metadata only: PR-AUC `0.9937680386465483`, threshold `0.5128232795323753` and 909 alerts are all bit-identical, and round-trip verification measures 0. **No modelling choice changed** | §7.7, §8 |
| 2026-09-21 | **Test alert rate 2.09% breaches the 2% review budget — reported, not fixed** | The threshold is frozen on validation (1.37% there) and §7.11 forbids re-tuning it on test, so the breach is published rather than corrected. It is prevalence, not drift: test carries 2.01% fraud against validation's 1.34% (2,075 of 102,987 rows), 1.51x as much, and at 99.3% recall the alert count tracks the fraud count almost exactly. E2 (2.08%) and E3 (2.10%) overshoot by the same margin, ruling out anything specific to the blend. In production this is precisely what a budget is for: it surfaces as a capacity breach and the threshold rises in the next model version, trading recall for load | §7.7, §7.8, §7.11 |
| 2026-09-21 | **HOLD was disabled before the test split was read, and nothing was rebuilt afterwards** | Verifiable rather than asserted: `models/v1/metadata.json` has `created_at` 01:48:13Z with the `thresholds` block already disabled, the single `reports/test_runs.log` line is 01:59:13Z, and every model binary (`xgb.ubj`, `iforest.joblib`, `anomaly_quantiles.npy`, `feature_stats.json`) still carries its 01:48 timestamp — only `metadata.json` was touched, by the run filling `metrics.test`. The test evaluation applied the stored policy and recorded `enabled: false, source: "policy"`, 0 HOLD decisions | §7.11, §8 |
| 2026-09-21 | `metadata.git_commit` records HEAD at build time and does not flag a dirty tree | `models/v1` was built at 01:48 from a working tree that was committed minutes later as `0d30e50`, so the metadata says `5ba2675` while `test_runs.log` says `0d30e50`. Harmless here — the diff between them changed only how the HOLD block is *reported*, the binaries round-trip to 0, and every recorded number reconciles — but the field overstates its own precision. A `git_dirty` flag would fix it; adding one now would mean rebuilding `v1` after test has been read, which is exactly what must not happen. Noted for `v2` | §8 |
| 2026-09-20 | Numeric knobs not fixed by the plan (diurnal peak sigmas, Zipf exponent, decline-vs-amount exponent, office group Pareto alpha, category medians/shares) were chosen here | §4.3-§4.5 specifies structure and targets, not every constant. These are tunable until the Phase 1 freeze, then fixed | §4.3, §4.8 |

---

## How to update this file

After finishing a task:
1. Tick its box in *Remaining work*.
2. Add a row to *Completed* with the date and the evidence (file, report or command).
3. Rewrite *Currently working on* and *Next up*.
4. Update the counters at the top and the *Current phase* line.
5. At a phase end: confirm `make lint && make test` (+ `make test-redis` from Phase 6), commit, tag `phase-N`.
6. Log anything that diverged from `PLAN.md` in *Decisions and deviations*.
