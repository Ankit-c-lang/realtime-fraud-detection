# PROGRESS — realtime-fraud-detection

Living status file. **Update it at the end of every completed task**, together with the commit for
that task. Source of truth for *what* to build is `PLAN.md`; this file only tracks *where we are*.

- **Plan version:** v1 (2026-09-16) · **Started:** 2026-09-20
- **Current phase:** Phase 1 — Simulator (PLAN §17, Phase 1) · Phase 0 complete, tagged `phase-0`
- **Phase 1 progress:** 3 / 8 tasks
- **Overall:** 11 / 73 must-have tasks done (+ 7 nice-to-have, not counted) · Phase 0: 7 / 7 ✅

---

## Currently working on

**Phase 1 · Task 4 — `src/fraud/sim/patterns.py` (PLAN §4.5, prompt P1.3).**
The four fraud injectors, each returning events plus label rows: VELOCITY, ATO,
CARD_TESTING and RING. Ring mule accounts are created here (their ages are relative to
each ring's start), along with the ring constraints: 30% device reuse from a ring active
in the last 30 days, >=12 rings starting inside the test window with >=4 of those reusing
a device, and >=20 rings starting and finishing inside training.

Labels carry `fraud_type`, `attack_id`, `ring_id` and `label_available_at`
(= `event_time` + 14 days). Victims' other transactions stay legitimate.

Blocked on nothing.

---

## Next up (in order)

1. **P1.3** — `src/fraud/sim/patterns.py` + `tests/test_sim_patterns.py` (see *Currently working on*).
2. **P1.4** — `src/fraud/sim/generate.py`: assemble, stable-sort by `event_time`, assign `txn_id`, write the four Parquet files and `manifest.json`. **Re-measure the tiny end-to-end runtime here** and decide whether the `sim_tiny.yaml` 90-day calendar survives (see deviations).
3. **P1.5** — `src/fraud/sim/checks.py` + `reports/sim_report.md` (§4.8).
4. Freeze `configs/sim.yaml`, record its sha256 in the report, tag `sim-v1`.

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

### Phase 1 — Simulator (Day 1 pm → Day 3 midday, ~16 h, 5/10) — IN PROGRESS
- [x] 1. Configs: `sim.yaml`, `sim_tiny.yaml`, `cities.csv` (15 IN + 8 intl), `categories.yaml`, `splits.yaml`
- [x] 2. `sim/population.py` — accounts, merchants, devices, IP pools
- [x] 3. `sim/legit.py` — legitimate behaviour + all hard negatives (§4.4)
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
| 2026-09-20 | CI pins `actions/checkout@v7` and `astral-sh/setup-uv@v10`, not the plan's v5/v6 | §14 says to use the current major version; v7 and v10 are current as of today | §14 |
| 2026-09-20 | CI runs `make lint` / `make test` instead of the raw `uv run ruff`/`pytest` lines | Keeps the CI gates identical to the local ones so they cannot drift. The `uv sync --frozen` step is kept exactly as the plan has it | §14 |
| 2026-09-20 | `setup-uv` pinned to the exact `v10.1.0`, not a floating major | The project publishes floating major tags only through v7; `@v10` does not resolve and failed the first CI run | §14, §15.3 |
| 2026-09-20 | `sim_tiny.yaml` keeps the full 90-day calendar, shrinking only population and attack volumes | Lets `configs/splits.yaml` apply unchanged so every structural check runs against the tiny config too. **Provisional against §13, which wants the unit suite fast: keep it only while the completed tiny simulator still builds in seconds during routine `make test`. Re-measure once `generate.py` lands (Phase 1 task 5); if the tiny end-to-end build stops being a seconds-scale operation, shorten its calendar and give it its own splits.** Today: tiny population build well under 1 s, whole fast suite 1.3 s | §4.8, §13 |
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
