# Real-Time Transaction Fraud Detection System: Build Plan (v1)

**One-line description:** a near-real-time fraud monitoring pipeline that scores a stream of card transactions using behavioural, graph-based and anomaly signals. It puts compromised cards and accounts on hold and explains every alert. Built on Redis Streams, NetworkX, XGBoost, Isolation Forest, DuckDB and FastAPI.

**Repository:** `realtime-fraud-detection` · **Python package:** `fraud` · **Plan version:** v1 (2026-09-16)

**How to use this file:** keep it in the repo root next to `CLAUDE.md`. The Claude Code prompts in §18 refer to sections by number (for example §5.2). When a decision changes, edit that section and bump the plan version. Never let the code and this file drift apart.

---

## 0. Framing and locked decisions

### 0.1 What this project owns (and what it must not duplicate)

| | Delivery Delay Prediction Platform (Project 2) | This project (Project 3) |
|---|---|---|
| Interview question it answers | How does a model reach production and get replaced safely? | How do you score events as they arrive, and catch fraud that no single transaction reveals? |
| Data | Real public dataset (Olist), batch ETL | Simulated event stream with injected fraud patterns |
| Storage | PostgreSQL feature warehouse | Redis for live state; Parquet + DuckDB for history and analytics |
| Features | Point-in-time aggregates through a fitted preprocessing artifact | Per-event incremental state plus scheduled graph snapshots |
| Modelling | Tuned gradient-boosting blend with calibration | One lightly tuned XGBoost plus a label-free Isolation Forest |
| Lifecycle | MLflow tracking and registry, promotion gate, CI/CD to GHCR | Versioned model folder, minimal CI, no CD |
| Signature tests | Train/serve parity of one fitted artifact | Replay parity across two state backends; safety under duplicate delivery |
| The "product" | The serving container | The stream processor (FastAPI is a thin layer on top) |

Shared tools (XGBoost, SHAP, FastAPI, Docker, time-based splits) are baseline tools. Keep them out of this project's headline.

Keep modelling deliberately light here: no Optuna, no CatBoost/LightGBM, no calibration story, no MLflow.

### 0.2 Locked decisions

| ID | Decision | Consequence |
|---|---|---|
| D1 | **Async stream monitoring** | Events are scored after authorization. There are three outcomes: `ALLOW`; `REVIEW` (analyst queue); `HOLD` (freeze the card/account for subsequent activity). The payment being scored is never "blocked", because it has already happened. |
| D2 | **14 days, near full-time** (~8 focused hours/day, ~110 h total) | Scope in §2 is sized to this. The cut list (§2.4) is agreed in advance. |
| D3 | Synthetic data from our own simulator | Public fraud datasets rarely carry usable device/IP linkage, and the ring pattern depends on it. |
| D4 | ₹0 budget, local-only deployment | Deployment is `docker compose up` on the VM plus a recorded demo. No cloud. |
| D5 | Ubuntu 24.04 VM (VMware) over VS Code Remote-SSH; Claude Code in the VS Code terminal; Docker Engine inside the VM | All instructions assume Linux. Hardware is an i5-1235U with 16 GB RAM and integrated GPU only, so everything runs on CPU with small tuning budgets. |
| D6 | Portfolio boundary (§0.1) | MLflow, Optuna, CatBoost/LightGBM, PostgreSQL, GHCR and CD stay out of this repo. |

### 0.3 What "real-time" means here

"Near-real-time" means an event is scored within a measured number of milliseconds (p95) of entering the stream, on the hardware above. Payment authorization is out of scope.

Say this directly in interviews:

> "This is post-authorization monitoring. It can't stop the payment it scores. It stops what comes next, which is how card testing and account takeover are usually contained."

The CV may say "real-time" only if the measured p95 is well under a second and the README states that this is post-authorization monitoring.

### 0.4 Honesty rules

1. Every number in the README or CV comes from a committed script. It is recorded in `reports/` along with the command that produced it.
2. Throughput and latency numbers always state the hardware and the VM allocation.
3. The simulator config is frozen before any model is trained (§4.8). Metrics are never improved by editing the generator.
4. The README states the synthetic-data limitation on its first screen.
5. Never say "production-grade". "Production-style patterns" is fine where true (consumer groups, idempotent processing, versioned artifacts).
6. The README describes the environment you actually used: an Ubuntu VM over Remote-SSH.

### 0.5 Working defaults (ASSUMPTION — CONFIRM LATER)

These were proposed in the review and not objected to, so they are the working defaults. To change one, edit the config or section listed.

| # | Default | Where |
|---|---|---|
| A1 | Indian card payments in ₹. One card per account, so `account_id` is the card identity. Some merchants and IPs are international. | §4 |
| A2 | ~500K transactions, 90 simulated days, ~22K accounts, 1.2-1.8% fraud | `configs/sim.yaml` |
| A3 | Time-based split with a burn-in period; some rings start only in the test window | §4.7 |
| A4 | Account-to-account graph built from shared devices and IPs; caps on how many accounts one device/IP may link; daily snapshots; 30-day edge lookback; 14-day label delay | §6 |
| A5 | Weighted blend, with the weight chosen by leave-one-pattern-out validation | §7.6 |
| A6 | Small Parquet files written in batches, one writer per directory; DuckDB reads them read-only | §9.4 |
| A7 | Versioned `models/<version>/` folder; no MLflow | §8 |
| A8 | XGBoost's native contributions become reason codes online; the `shap` package is used offline only | §7.10 |
| A9 | One scorer that reads in small batches; splitting the stream into partitions is NICE TO HAVE | §9.2 |
| A10 | Live demo: load saved state into Redis, then replay the test window on a sped-up clock | §9.1, §9.5 |
| A11 | GitHub Actions CI (lint, unit tests, Redis integration tests, image build); no CD | §14 |
| A12 | Local deployment plus a recorded demo; no authentication | §12 |
| A13 | Python 3.12 + uv + lockfile | §15 |
| A14 | Minimal Streamlit dashboard; not a CV line | §11 |

---

## 1. Corrections to the original plan

| Original item | Problem | Corrected design | Section |
|---|---|---|---|
| Account-Device/Merchant/IP graph plus clustering coefficient | That graph is bipartite (edges only run between accounts and entities), so it has no triangles and clustering is 0 for every node | Build an account-to-account graph: two accounts are linked if they share a device or IP | §6.1 |
| Merchant edges | Popular merchants link thousands of accounts into one giant cluster | Keep merchants out of the graph; use a merchant fan-out feature instead | §5.2, §6.1 |
| Shared IPs (carrier NAT, colleges) and card-testing devices | These link thousands of unrelated accounts, which creates fake "rings" | Cap how many accounts one device or IP may link, before the graph is built | §6.1 |
| Plain PageRank | On an undirected graph it closely tracks degree | Personalized PageRank seeded from known fraud, with a 14-day label delay | §6.3 |
| Community ID as a feature | It is an arbitrary label that changes between runs | Use community-level aggregates instead | §6.3 |
| One graph built over all data | Training rows would see edges from the future | Daily snapshots attached with a point-in-time (ASOF) join | §6.2, §6.4 |
| Pandas features in Phase 3, Redis features in Phase 6 | Two implementations that will quietly disagree | One `FeatureEngine`, two state stores, and a parity test | §5.4 |
| Random train/test split | Bursts of the same attack, and the same accounts, leak across the split | Time-based split with burn-in; some rings exist only in the test window | §4.7 |
| XGBoost on raw columns as the baseline | A weak reference point | A rules baseline (R1-R4) | §7.3 |
| Blend weight tuned on validation | If validation holds only known patterns, the tuned weight pushes Isolation Forest to ~0 | Pick the weight by leave-one-pattern-out validation | §7.6 |
| `decision: BLOCK` on a stream | The payment has already been processed | `ALLOW` / `REVIEW` / `HOLD` | §0.2, §7.7 |
| Redis concepts listed "to learn" | Ordering, retries and duplicates were never designed | Single ordered scorer, idempotent commit per event, pending-message recovery, dead-letter queue (DLQ), backpressure | §9 |
| One DuckDB file shared by several containers | DuckDB allows one read-write process or many read-only ones, not both at once | Parquet files written in batches plus read-only DuckDB | §9.4 |
| SHAP library in the serving path | Heavy dependency, and it only explains the XGBoost half of the score | Native `pred_contribs` turned into reason codes, plus a separate anomaly reason | §7.10 |
| API input `{account_id, amount, device_id, location}` | Missing merchant, timestamp, IDs and status | Full event schema | §3.5, §10 |
| Simulator done on Day 1 | Too optimistic; realistic legitimate behaviour is most of the work | 2.5 days | §17 |
| PyOD | Redundant; scikit-learn already includes Isolation Forest | Dropped | §3.8 |

**Refinements to the review:**

- *Idempotency.* The review suggested making velocity counts idempotent by using `txn_id` as the sorted-set member. That only protects counts: running mean/std and "seen" sets would still double-update on redelivery. So the plan commits each event atomically together with a stored feature record, and a redelivered event returns the stored record (§5.4, §9.3).
- *`card_id`.* The review suggested a separate `card_id`. With one card per account (A1) it adds a join and no signal, so `account_id` serves as the card identity.

---

## 2. Scope

### 2.1 MUST HAVE (the project is incomplete without these)

1. **Simulator.** 4 fraud patterns, hard negatives, labels with an availability time, a validation report, and a frozen config (§4).
2. **`FeatureEngine`.** 30 per-event ("hot") features, an in-memory store and a Redis store, plus a parity test and an idempotency test (§5).
3. **Graph snapshots.** 6 graph ("warm") features, a two-step point-in-time join, and a live refresh job driven by the sink's watermark (the latest event time written; §6).
4. **Modelling.** Rules baseline, XGBoost (with and without graph features), Isolation Forest, a blend weight chosen by leave-one-pattern-out validation, and a two-tier decision policy. Experiments E1-E6 feed a results table (§7).
5. **Explanations.** Reason codes from native XGBoost contributions, plus an anomaly reason (§7.10).
6. **Versioned model folder** with a metadata file; every scored row is stamped with its version (§8).
7. **Streaming pipeline.**
   - Replayer: paces events and applies backpressure.
   - Scorer: consumer group, micro-batch reads, idempotent commits, pending-message recovery, DLQ, acknowledge only after the sink has written.
   - Parquet sink with a watermark, a backfill job, and a check that re-scores live output offline (§9).
8. **FastAPI** with `/health`, `/score` (read-only), `/alerts`, `/transactions/{id}` and `/metrics` (§10).
9. **Minimal Streamlit dashboard** (§11).
10. **Docker Compose** running the whole stack with one command (§12).
11. **Tests** spread across phases, plus a GitHub Actions CI workflow (§13, §14).
12. **README** covering architecture, results, benchmark, limitations and a recorded demo (§17, Phase 10).

### 2.2 NICE TO HAVE (only after every MUST item is done, in this order)

1. **Partitioned streams.** Split the stream into K = 2-4 sub-streams by hashing the account ID, with one scorer per partition, plus a scaling benchmark (§9.7).
2. **Ego-graph panel.** A dashboard view of a flagged account and its direct neighbours, drawn with `st.graphviz_chart` and no extra dependency.
3. **Parquet compaction job.** Merge the many small files in `data/scored/`.
4. **Label-noise knob.** Leave some fraud unlabelled and show how sensitive the results are.
5. **Relabel variant** of the withheld-pattern experiment: withheld rows are labelled 0 instead of dropped.
6. **Strict snapshot alignment.** The scorer waits at each day boundary until the new snapshot is published.
7. **Slim serving image** using the `xgboost-cpu` wheel, if it is still published for your XGBoost version.

### 2.3 DO NOT BUILD

| Technology or feature | Why not |
|---|---|
| MLflow, Optuna, CatBoost/LightGBM, calibration story, PostgreSQL, GHCR publishing, CD | Owned by Delivery Delay; repeating them dilutes the portfolio |
| Kafka / Redpanda | Redis Streams teaches the same concepts with one container; Kafka goes on the "at 100x scale" list (§9.7) |
| Spark, Flink | 500K events fit in memory on one machine |
| Airflow, Prefect | No schedule-heavy DAG; Makefile plus Compose is enough |
| Neo4j or another graph database | NetworkX handles snapshots of this size; a graph server adds a query language to learn |
| GNNs or graph embeddings | Weeks of work, hard to defend, and needs a GPU |
| Incremental or streaming community detection | Research-grade; daily snapshots are the defensible design |
| Feast or another feature store product | The two-store `FeatureEngine` is the lesson; a product hides it |
| Kubernetes, Terraform, cloud deployment | ₹0 budget; no interview value beyond Compose for this project |
| Authentication, rate limiting | Local demo |
| Prometheus + Grafana | `/metrics` JSON plus the dashboard cover the demo |
| LLM "fraud analyst" features | Off-scope; Project 1 owns GenAI |
| Separate microservices for the API | One API service is the right size |

### 2.4 Cut list and checkpoints

**Checkpoints (evening of the named day):**

- **End of Day 5:** E2 exists (XGBoost on hot features, validation metrics).
- **End of Day 8:** `models/v1` and `reports/results.md` exist.
- **End of Day 10:** the test window replays end to end through Redis, and the re-score check passes.

If a checkpoint is missed, apply cuts **in this order** until back on schedule:

1. Dashboard replay scorecard panel (keep the other panels).
2. `GET /transactions/{id}` (keep `/alerts`).
3. DLQ (keep pending-message recovery).
4. Leave-one-pattern-out on 2 patterns (card testing, rings) instead of 4. The weight is then chosen on those 2.
5. Live graph refresh: switch to weekly snapshots instead of daily.
6. CI `image-build` job.

**Never cut:**

- time-based split
- parity test
- idempotent commit
- ack-after-flush
- graph-feature ablation
- leave-one-pattern-out evidence (at least 1 pattern)
- measured latency and throughput
- honest README

---

## 3. Architecture

### 3.1 Requirements and load estimate

**Functional requirements**

- Score every transaction event from the stream.
- Put cards/accounts on HOLD after high-confidence detections.
- Queue medium-risk events for REVIEW.
- Explain every alert.
- Expose health, metrics, alerts and on-demand scoring.
- Show the system live on a dashboard.

**Non-functional requirements**

| Concern | Target | How it is met |
|---|---|---|
| Correctness | Features identical offline and online; nothing leaks from the future | One `FeatureEngine`, a parity test, point-in-time joins (§5, §6) |
| Delivery | No event lost; no double counting after a crash | Consumer group, ack-after-flush, idempotent per-event commit (§9.3) |
| Latency | Measured, not promised: p50/p95/p99 from enqueue to scored | `scored_ts - ingest_ts` stored on every row (§9.6) |
| Throughput | Measured capacity of one Python scorer | Benchmark with the replayer running without sleeps (`--max`) (§9.6) |
| Bursts | The producer slows down when the scorer falls behind | Backpressure based on group lag (§9.1) |
| Cost | ₹0 | Everything local |
| Memory | Whole stack comfortably under ~4 GB (estimate; verify with `docker stats`) | Capped stream, trimmed sorted sets |

**Load estimate**

- Offline: ~500K events.
- Live replay (test window, 18 simulated days): ~100K events.
- At a 3,600x speed-up (1 simulated hour per real second) the replay takes about 7 minutes, averaging ~230 events/s with bursts during card-testing attacks and diurnal peaks.
- A single Python scorer should manage low thousands of events/s. This is an expectation to measure, not a claim.

**Redis memory estimate**

- ~22K account state blobs at ~1-3 KB each: tens of MB.
- Entity sorted sets (devices, IPs, merchants), a few hundred thousand members in total: tens of MB.
- Stream capped at 500K entries.
- Total: well under 1 GB.

### 3.2 Diagram

```
OFFLINE  (host venv, `make` targets)
------------------------------------------------------------------------------------------
 sim.generate --> data/raw/{events,accounts,merchants,labels}.parquet
                      |                                   |
                      v                                   v
 features.replay (FeatureEngine + InMemoryStore)     graph.snapshots (DuckDB projection + NetworkX)
      |   '--> data/state/checkpoint_<test_start>.json.gz        |
      v                                                          v
 data/features/hot_features.parquet            data/graph/offline/snapshot_ts=*/part.parquet
      '--------------------> graph.join (DuckDB, two-step ASOF) <-----'
                                     |
                                     v
                     data/features/training_table.parquet
                                     |
          modeling: rules | XGBoost | Isolation Forest | blend (LOPO weight) | thresholds
                                     |
                                     v
                 models/<version>/ + models/CURRENT + reports/results.md

ONLINE  (docker compose)
------------------------------------------------------------------------------------------
 backfill (runs once): checkpoint -> Redis state; offline snapshot -> Redis graph keys;
                       create stream + consumer group; write compatibility markers

 replayer --XADD--> [txn:events] --XREADGROUP (group "scorers")--> scorer
 (test window,        Redis Stream                                  | FeatureEngine + RedisStore (atomic commit per event)
  labels stripped,                                                  | graph lookup (Redis)
  paced clock)                                                      | RiskModel: XGBoost + IsolationForest -> risk -> decision
     ^                                                              | reason codes for REVIEW/HOLD
     '-- backpressure: pause while group lag is high                |--> data/scored/date=*/part-*.parquet  (single writer)
                                                                    |--> alerts:recent, metrics:scorer, sink:watermark
                                                                    '--> [txn:dlq]  (poison messages)

 graph-refresh: when sink:watermark passes the next day boundary T
      --> snapshot T from history + scored events with event_time < T (+ labels available before T)
      --> data/graph/live/snapshot_ts=T/  and  Redis graph:{T}:acct:*  ;  ZADD graph:published T

 api (FastAPI):         /health  /score (read-only)  /alerts  /transactions/{id}  /metrics
 dashboard (Streamlit): DuckDB read-only over data/scored/ + calls to the api
```

### 3.3 Components

Complexity ratings: L = low, M = medium.

| Component | Purpose | Technology | Why it is included | Learn first | Complexity | Necessary |
|---|---|---|---|---|---|---|
| Simulator | Events, reference tables, labels | NumPy, pandas, PyArrow (Faker optional, names only) | We need device/IP linkage and known patterns | Poisson arrivals, lognormal amounts, seeded randomness | M | Yes |
| `FeatureEngine` | 30 hot features per event | Pure Python + `sortedcontainers` | One implementation used offline and online | Event-time windows, running mean/variance | M | Yes |
| `InMemoryStore` | Offline replay backend | dicts, sorted lists | Fast training-set build; reference for parity | None | L | Yes |
| `RedisStore` | Live state backend | redis-py, Redis | Shared, durable, idempotent state | Redis types, pipelines, MULTI/EXEC | M | Yes |
| Graph snapshots | 6 warm features | DuckDB SQL + NetworkX | Fraud-ring detection | Projections, Louvain, (personalized) PageRank | M | Yes |
| Point-in-time join | Attach snapshots without leakage | DuckDB `ASOF JOIN` | Leak-free training table | As-of joins | L | Yes |
| Rules baseline | Reference point | Python | "How much does ML add over rules?" | Precision/recall | L | Yes |
| XGBoost | Classifier for known patterns | `xgboost` (hist) | Strong on tabular data; already familiar | Early stopping, categorical support | L | Yes |
| Isolation Forest | Label-free novelty score | scikit-learn | Catches patterns it was never labelled for | Isolation trees, score direction | L | Yes |
| Blend + decision policy | Final risk score and actions | NumPy | Combines both models; thresholds respect analyst capacity | PR curves, alert budgets | L-M | Yes |
| Reason codes | Explain each alert | XGBoost `pred_contribs` | Analyst-facing "why was this flagged?" | SHAP additivity | L | Yes |
| Model folder | Versioned artifacts | UBJSON, joblib, JSON | Reproducibility without MLflow | Serialization pitfalls | L | Yes |
| Redis Streams | Event log and delivery | Redis | Consumer groups, acks, recovery, replay | `XADD`/`XREADGROUP`/`XACK`/`XPENDING`/`XAUTOCLAIM` | M | Yes |
| Replayer | Producer with pacing and backpressure | redis-py | Deterministic, demoable live run | Lag, pacing | L | Yes |
| Scorer service | Consume, featurize, score, write | Python | The core of this project | Micro-batching, at-least-once delivery | M | Yes |
| Parquet sink | Durable output | PyArrow | One writer, many readers | Atomic rename, partitioning | L | Yes |
| Live graph refresh | Publish snapshots during replay | Python + DuckDB + NetworkX | The warm path in the live system | Watermarks | M | Yes |
| FastAPI | On-demand scoring and reads | FastAPI, Pydantic v2, uvicorn | Standard serving interface | Lifespan, dependencies | L | Yes |
| Dashboard | Show the system live | Streamlit | A live system has to be visible | `st.fragment`, caching | L | Yes (minimal) |
| Docker Compose | Run the stack | Docker Engine, Compose v2 | One-command demo | Healthchecks, `depends_on` conditions | L | Yes |
| CI | Lint, tests, image build | GitHub Actions | Hygiene | Service containers | L | Yes (minimal) |

### 3.4 Hot path vs warm path

Features are split by how cheaply they can be updated.

| Path | Features | Updated | Where computed | Freshness |
|---|---|---|---|---|
| Hot | 30 behavioural, novelty and fan-out features | Every event | `FeatureEngine` inside the scorer (Redis state) | Includes every earlier event |
| Warm | 6 graph features | Once per simulated day | Graph job (DuckDB + NetworkX) | Last published snapshot; staleness is measured |

This is the standard split for features that are too expensive to update per event. "Which features can you compute online, and how do you stop batch features leaking or going stale?" is a question you should be able to answer from this table.

### 3.5 Event schema (stream message, `schema_version = 1`)

| Field | Type | Notes |
|---|---|---|
| `txn_id` | str | Unique; assigned by the simulator after sorting by time |
| `event_time` | ISO-8601 string (IST, no offset) | Event time. All windows use this, never wall-clock time |
| `account_id` | str | The card account (one card per account) |
| `merchant_id` | str | |
| `merchant_category` | str | One of the fixed categories in `configs/categories.yaml` |
| `amount` | float | ₹, 2 decimals, > 0 |
| `channel` | `POS` or `ONLINE` | |
| `device_id` | str | |
| `ip` | str | IPv4 |
| `lat`, `lon` | float | Merchant location for POS; IP geolocation for ONLINE |
| `city`, `country` | str | Same source as `lat`/`lon` |
| `status` | `APPROVED` or `DECLINED` | The authorization outcome is known when the event is emitted |
| `ingest_ts` | int (epoch ms) | Added by the replayer; used for latency |
| `schema_version` | int | Must be 1 |

**Labels are never in the stream.** The replayer asserts this, and a test checks it (§13).

### 3.6 Redis key design

| Key | Type | Content | Written by | Lifetime |
|---|---|---|---|---|
| `txn:events` | stream | Incoming events; consumer group `scorers` | replayer | `MAXLEN ~ 500000` |
| `txn:dlq` | stream | Poison messages plus the error text | scorer | `MAXLEN ~ 10000` |
| `state:acct:{account_id}` | string (JSON) | `AccountState` blob (§5.3) | scorer, backfill | Persistent |
| `ent:dev:{device_id}` | zset | Member = account_id, score = last event time (epoch s) | scorer, backfill | Trimmed to 30 days |
| `ent:ip:{ip}` | zset | Same | scorer, backfill | Trimmed to 30 days |
| `ent:mer:{merchant_id}` | zset | Same | scorer, backfill | Trimmed to 30 days |
| `feat:{txn_id}` | string (JSON) | Committed feature record (idempotency) | scorer | 48 h TTL |
| `graph:published` | zset | Member/score = snapshot_ts (epoch s) | graph-refresh, backfill | Persistent |
| `graph:{T}:acct:{account_id}` | hash | 6 graph features for snapshot T | graph-refresh, backfill | 3-day TTL |
| `hold:acct:{account_id}` | string | Account/card on HOLD (output annotation only, never a model input) | scorer | 7-day TTL |
| `alerts:recent` | list | Latest 500 alerts (JSON) | scorer | `LTRIM` |
| `metrics:scorer` | hash | Counters and heartbeat | scorer | Persistent |
| `metrics:latency_ms` | list | Last 1,000 end-to-end latencies | scorer | `LTRIM` |
| `sink:watermark` | string | Largest `event_time` flushed to Parquet | scorer | Persistent |
| `meta:state_version` | string | `FEATURE_SPEC_VERSION` the state was built for | backfill | Persistent |
| `backfill:done:{spec}:{checkpoint}` | string | Marker that makes backfill idempotent | backfill | Persistent |

### 3.7 Storage layout

```
data/                                    # gitignored
  raw/          events.parquet  accounts.parquet  merchants.parquet  labels.parquet
  features/     hot_features.parquet  training_table.parquet
  state/        checkpoint_<test_start>.json.gz          # in-memory state at the test boundary
  graph/offline/snapshot_ts=<T>/part.parquet  +  calendar.parquet
  graph/live/   snapshot_ts=<T>/part.parquet             # written only by graph-refresh
  scored/       date=<YYYY-MM-DD>/part-<consumer>-<seq>.parquet   # written only by the scorer
models/<version>/ ...  and  models/CURRENT                # gitignored except .gitkeep
reports/        sim_report.md  experiments/*.json  results.md  benchmark.md  figures/   # committed
```

**Rule:** every directory has exactly one writer process. Readers use DuckDB in read-only mode (an in-memory connection over Parquet), so there are no file locks.

### 3.8 Technology decisions (three options each)

| Choice | Picked | Option B (why not) | Option C (why not) |
|---|---|---|---|
| Event broker | **Redis Streams**: consumer groups, acks, pending list, replay; one container; the same Redis also holds state | Kafka/Redpanda: extra heavy service; partitions are its main gain, which is NICE here | In-process queue or file replay: no delivery semantics to design or discuss |
| Online state | **Redis** (JSON blobs + sorted sets): survives scorer restarts, readable by the API | Scorer process memory: lost on restart; the API can't read it | Local RocksDB/SQLite (Flink-style): more code, not shareable |
| Offline storage/query | **Parquet + DuckDB**: columnar, ASOF joins, no lock contention | Shared DuckDB file: one read-write process blocks the readers | PostgreSQL: Delivery Delay's store; row-oriented for analytics |
| Graph library | **NetworkX**: familiar, has Louvain and PageRank, fast enough at this size | igraph/graspologic: faster, but a new API for no gain here | Neo4j: server plus Cypher; overkill |
| Community detection | **Louvain** (built into NetworkX, seeded) | Label propagation: faster but less stable | Leiden: better quality, but an extra dependency |
| Graph risk feature | **Personalized PageRank with a label delay** | Plain PageRank: mostly repeats degree | GNN embeddings: weeks of work, hard to defend |
| Supervised model | **XGBoost** (hist, native categorical) | LightGBM: equivalent, and GBDT tuning is Delivery Delay's story anyway | Logistic regression: misses interactions such as "new device AND high speed" |
| Class imbalance | **No reweighting; threshold chosen within alert budget** | `scale_pos_weight`: distorts probabilities, which then get blended | SMOTE: synthetic rows on top of synthetic data; breaks time structure |
| Anomaly detector | **Isolation Forest** (scikit-learn, label-free) | PyOD (ECOD/COPOD): extra dependency; not in the CV | Autoencoder: tuning cost with no GPU |
| Combining models | **Weighted blend, weight chosen by leave-one-pattern-out** | Two-queue union that reserves x% of the budget for anomalies: fallback if the blend collapses | Stacking meta-model: needs more labelled data and overfits known patterns |
| Explanations | **XGBoost native contributions** | `shap.TreeExplainer` at serve time: heavy, with version friction | LIME: slow and unstable |
| API | **FastAPI** (Pydantic v2 validation) | Flask: no built-in validation | gRPC: overkill for a demo |
| Dashboard | **Streamlit** | Grafana + Prometheus: two more services, metrics only | React frontend: weeks of work |
| Packaging | **uv + `uv.lock`** | pip + requirements lockfile: slower, two files to keep in sync | Poetry: heavier, no advantage |
| Orchestration | **Makefile + Compose** | Airflow/Prefect: no scheduled DAG to justify them | Bash only: less discoverable |
| Model versioning | **Folder + `metadata.json` + `CURRENT`** | MLflow: Delivery Delay's domain | DVC: the data is regenerated deterministically, so not needed |

---

## 4. Simulator (data and problem definition)

### 4.1 Problem definition

| Item | Definition |
|---|---|
| Unit of prediction | One transaction event |
| Target | `is_fraud = 1` if the transaction belongs to one of the 4 injected patterns |
| Prediction time | Right after the authorization outcome is known (`status` is part of the event) |
| Outputs | Fraud probability, anomaly percentile, risk score, decision, reasons |
| Positive rate | ~1.2-1.8% of transactions |
| Business framing | Catch as much fraud as possible within a fixed analyst capacity (alert budget). HOLD is reserved for high-precision cases, because freezing a genuine customer's card is costly. |

**The four patterns, in interview language:**

- **Velocity abuse.** A compromised card is drained fast: many purchases within minutes.
- **Account takeover (ATO).** Someone logs in from a new device in another city or country and makes large purchases, often minutes after the real owner paid at home ("impossible travel").
- **Card testing.** A fraudster checks a batch of stolen cards with tiny charges at low-friction merchants (many get declined), then cashes out the cards that work.
- **Fraud ring.** A group of young mule accounts is operated from a few shared devices and IPs, cashing out through colluding merchants. Each transaction looks ordinary; the fraud only shows up in the connections between accounts.

### 4.2 Tables (simulator output)

**`events.parquet`** (sorted by `event_time`): every field in §3.5 except `ingest_ts` and `schema_version`, which the replayer adds.

**`accounts.parquet`** (reference data, no labels):

- `account_id`, `created_at`
- `home_city`, `home_lat`, `home_lon`
- `spend_level`

**`merchants.parquet`** (reference data):

- `merchant_id`, `name`, `category`
- `city`, `country`, `lat`, `lon`
- `is_online`, `created_at`

**`labels.parquet`** (read only by evaluation, the graph job's seeds and the dashboard scorecard; never by the scorer):

- `txn_id`
- `is_fraud` (0/1)
- `fraud_type` (`NONE`, `VELOCITY`, `ATO`, `CARD_TESTING`, `RING`)
- `attack_id`
- `ring_id` (nullable)
- `label_available_at` (`event_time` + 14 days)

**`manifest.json`**: seed, generator version, sha256 of `sim.yaml`, and row counts per table and per pattern.

### 4.3 Population (defaults in `configs/sim.yaml`)

| Knob | Default | Why |
|---|---|---|
| `seed` | 42 | Determinism |
| `start`, `days` | 2026-01-01 (IST), 90 | ~500K events at the rate below |
| `n_accounts` | 22,000 legitimate accounts: 90% exist at start (created 2023-2025); 10% are created during the simulation | New legitimate accounts stop "young account = fraud" from being trivially true |
| `activity_rate` | Lognormal per account, median 0.18/day; rescaled to hit `target_transactions = 500000` ± 5% | Mix of light and heavy users |
| `spend_level` | Lognormal, median 1.0, sigma 0.5 | Differences between accounts |
| Cities | 15 Indian cities (weighted) + 8 international cities (`configs/cities.csv` with approximate coordinates) | Home locations, travel, ATO |
| Merchants | 2,000 in 10 categories; popularity follows a Zipf distribution; ~35% online; ~5% international; 20 low-friction digital merchants; 60 colluding merchants created during the simulation | Hubs, testing targets, cash-out points |
| Categories | grocery, fuel, restaurants, pharmacy, utilities, ecommerce, electronics, travel, digital_goods, gift_cards_wallet. Each has a lognormal amount (median, sigma) and a POS/ONLINE mix in `configs/categories.yaml` | Fixed order, which also fixes the categorical codes |
| Devices | 85% of accounts have 1 personal device, 15% have 2; 5% upgrade their phone mid-simulation | "New device" is sometimes legitimate |
| Families | 8% of accounts are in groups of 2-4 sharing a household device (~40% of their transactions) and a home IP | Legitimate shared devices |
| Home IPs | One per account or family | |
| Carrier NAT | 40 IPs; 45% of accounts use them for ~50% of their online transactions | Huge shared IPs (dropped by the fan-out cap) |
| Office/college IPs | 60 IPs, group sizes 5-150 (heavy-tailed); 25% of accounts; weekdays 09:00-18:00 | Mid-size legitimate IP clusters |
| VPN | 2% of accounts send 10% of their online transactions through 10 foreign VPN IPs | Legitimate "impossible travel" |

### 4.4 Legitimate behaviour and hard negatives

Legitimate generation per account:

1. Daily counts are drawn from a Poisson distribution with a weekday factor.
2. Times follow a diurnal mixture (peaks ~13:00 and ~20:00, quiet 01:00-06:00). 5% of accounts are "night owls" with a shifted profile.
3. 70% of purchases go to the account's 5-15 regular merchants. The other 30% explore by popularity (home city plus online).
4. Amount = category median × spend level × lognormal noise.
5. POS location is the merchant's city. ONLINE location is the IP's geolocation.
6. 1.5% of transactions are randomly declined, slightly more often for large amounts.

| Hard negative | How it is generated | Which fraud signal it imitates |
|---|---|---|
| Travel | 4% of accounts take one 2-7 day trip (75% domestic, 25% international). Realistic gaps are enforced (≥ 2 h domestic, ≥ 6 h international). | Location jumps (ATO) |
| VPN use | Section 4.3; the device stays the same | Impossible travel (ATO) |
| Phone upgrade | New device, then consistent use | New device (ATO, velocity) |
| Shopping spree | 2% of account-days get 4-8 extra transactions within 90 minutes at 2-5 merchants | Velocity |
| Micro-payment bursts | 3% of accounts have 1-3 episodes of 5-12 transactions under ₹100 within 60 minutes, at a regular merchant, from their own device | Card testing |
| Legitimate customers of low-friction merchants | Small digital purchases, mostly approved | Card testing |
| Families and offices | Sections 4.3 | Shared device/IP (ring) |
| New legitimate accounts | Created during the simulation; first transactions come from a device new to the account | Young accounts (ring) |
| Small legitimate merchants | Long tail of the Zipf popularity distribution | Colluding merchants (ring) |

### 4.5 Fraud patterns (defaults; target total ≈ 1.5% of transactions)

| Pattern | Mechanics | Signals it leaves | Default volume |
|---|---|---|---|
| `VELOCITY` | Victim: a random active legitimate account. Device: a new device (70%) or the victim's own (30%). 10-30 transactions in 3-15 minutes at 1-4 merchants (ecommerce, digital goods, gift cards). Amounts: median ₹2,500, capped at ₹20,000. 5-15% declined. | `acct_cnt_5m`, `acct_merchants_1h`, `amount_zscore`, `new_device` | ~60 attacks, ~1,500 transactions |
| `ATO` | Victim: an account with ≥ 10 earlier transactions. New attacker device. IP is foreign (60%) or in another Indian city (40%). First fraud transaction comes 5-60 minutes after a legitimate POS purchase at home. 3-8 transactions over 30-180 minutes at electronics, gift-card or travel merchants. Amount 3-10x the victim's mean, capped at ₹150,000. ~10% declined. | `geo_speed_kmh`, `new_device`, `new_city`, `amount_to_mean`, `is_international` | ~120 attacks, ~600 transactions |
| `CARD_TESTING` | One attacker device plus IP (residential 50%, VPN 50%) tests 20-60 random victim accounts at 1-2 low-friction merchants within 20-60 minutes. Amounts ₹1-50; 40-60% declined. 30% of the approved victims get a cash-out of ₹5,000-40,000 at electronics or gift-card merchants 1-12 hours later, from the same device/IP. | `dev_accts_1h`, `ip_accts_1h`, `acct_declines_1h`, `acct_small_1h`, `new_device` | ~50 attacks, ~2,300 transactions |
| `RING` | 6-15 accounts created 3-30 days before the ring starts. They share 2-4 devices and 1-3 residential IPs and stay active for 5-25 days. Each account makes 6-12 transactions: 60% at 1-3 colluding merchants, 40% at popular merchants. **Amounts, hours and approval rates look normal.** | Graph features, `account_age_days`, `mer_accts_30d`, `dev_accts_30d` | ~45 rings, ~3,000 transactions |

**Ring constraints (these are what make the evaluation meaningful):**

- 30% of rings reuse at least one device from a ring active in the previous 30 days.
- At least 12 rings **start inside the test window**, and at least 4 of those reuse a device.
- At least 20 rings start and finish inside the training period.

**Labelling:**

- Every transaction produced by an attack is labelled with its `fraud_type` and `attack_id`.
- Every transaction by a ring account is labelled `RING`, with `ring_id = attack_id`.
- The victims' other transactions stay legitimate.

### 4.6 Labels and label delay

`label_available_at = event_time + 14 days` in every split. This simulates chargebacks arriving late.

Only two things use labels as inputs:

- the graph job's personalized PageRank seeds and community fraud share, and only labels available before the snapshot time (§6.2);
- evaluation code.

The dashboard's replay scorecard uses labels for display only and is clearly marked as such.

### 4.7 Time splits (`configs/splits.yaml`, half-open intervals)

| Split | Interval | Days | Use |
|---|---|---|---|
| `burn_in` | [2026-01-01, 2026-01-15) | 1-14 | Builds up state; rows are excluded from training and evaluation |
| `train` | [2026-01-15, 2026-02-23) | 15-53 | Model fitting; Isolation Forest fitting; anomaly reference quantiles |
| `early_stop` | [2026-02-23, 2026-03-02) | 54-60 | XGBoost early stopping only |
| `valid` | [2026-03-02, 2026-03-14) | 61-72 | Model selection, blend weight, thresholds |
| `test` | [2026-03-14, 2026-04-01) | 73-90 | Final evaluation (§7.11); also the live replay window |

Approximate row counts: burn-in ~78K, train ~217K, early_stop ~39K, valid ~67K, test ~100K.

`splits.yaml` is the single source of truth. Nothing else hard-codes a date.

### 4.8 Validation checks and freeze rule

`fraud.sim.checks` writes `reports/sim_report.md`, and tests assert the same properties on a small config:

- Fraud prevalence is within [1.0%, 2.0%]; each pattern's count is within ±30% of its target.
- CARD_TESTING: every attack device touches ≥ 20 distinct accounts within 60 minutes.
- ATO: ≥ 90% of attacks have `geo_speed_kmh > 900` at the first fraud transaction.
- RING: the constraints in §4.5 hold.
- Hard negatives exist in meaningful numbers. The thresholds are derived from config: VPN impossible-travel events, family-shared devices, new legitimate accounts, micro-bursts.
- `events.parquet` contains exactly the §4.2 columns and **no label columns**.
- `txn_id` is unique; events are sorted; no event happens before its account's `created_at`.
- Two runs with the same seed produce identical file hashes (small config).

**Freeze rule.**

- At the end of Phase 1, record `sha256(configs/sim.yaml)` in the report and tag the commit `sim-v1`. The same hash goes into every model's metadata.
- Only if validation PR-AUC in E1/E2 is suspiciously perfect (> 0.995) may you revise realism, and only **once** (`sim-v2`), with the reason written in the README.
- Never touch the generator after looking at test metrics.

**Implementation notes.**

- Use `numpy.random.SeedSequence(seed).spawn(...)` to give independent generators to the population, legitimate behaviour and each pattern.
- Vectorise within each account (a loop over ~22K accounts is fine; a loop over 500K events is not).
- Concatenate, stable-sort by `event_time`, then assign `txn_id = f"T{i:07d}"`.
- Write Parquet with zstd compression.
- Target runtime: under ~3 minutes on the VM.

---

## 5. Feature engine

### 5.1 Conventions (these are invariants; tests enforce them)

1. **Event time only.** Every window uses `event_time`, never wall-clock time. Timestamps are stored as epoch **milliseconds** (int) in state and in sorted-set scores. Redis scores are doubles, which represent these integers exactly.
2. **Half-open windows.** Windows are `[t - w, t)`, so the current event is never counted in its own window features. Its own values appear only in the context features (family A).
3. **Compute, then update.** Features are computed from the state *before* the event, then the state is updated. Both stores run the same code.
4. **Cold-start defaults.**
   - Counts are 0.
   - `amount_zscore = 0` when there are fewer than 3 prior approved transactions.
   - `amount_to_mean = 1.0` when there is no prior approved transaction.
   - `secs_since_last` = 30 days when there is no history.
   - `geo_speed_kmh = 0` when there is no previous event.
   - The model sees `acct_history_cnt`, so it knows when defaults are in play.
5. **Spending statistics** (mean/std) use prior **approved** transactions. Counts and windows use all statuses.
6. **Feature order** comes from `fraud.features.spec.FEATURE_NAMES`. Nothing else defines column order. `FEATURE_SPEC_VERSION = "fs1"`; bump it whenever a definition changes.
7. **Time zone.** Hours are IST, and the simulator writes IST timestamps.

### 5.2 Feature list (36 = 30 hot + 6 warm)

| # | Feature | Definition | Main target |
|---|---|---|---|
| **A. Context (stateless)** | | | |
| 1 | `log_amount` | ln(1 + amount) | all |
| 2 | `hour_of_day` | 0-23 | ATO, velocity |
| 3 | `is_night` | hour in [0, 5) | ATO |
| 4 | `is_online` | channel == ONLINE | card testing |
| 5 | `is_international` | country != IN | ATO |
| 6 | `merchant_category` | categorical, fixed level order from `categories.yaml` | all |
| **B. Account velocity** | | | |
| 7 | `acct_cnt_5m` | account transactions in [t-5m, t) | velocity |
| 8 | `acct_cnt_1h` | ... in [t-1h, t) | velocity |
| 9 | `acct_cnt_24h` | ... in [t-24h, t) | velocity |
| 10 | `acct_amt_24h` | sum of amounts in [t-24h, t) | velocity |
| 11 | `acct_merchants_1h` | distinct merchants in [t-1h, t) | velocity |
| 12 | `acct_declines_1h` | declined transactions in [t-1h, t) | card testing |
| 13 | `acct_small_1h` | transactions with amount < ₹100 in [t-1h, t) | card testing |
| **C. Behavioural deviation** | | | |
| 14 | `secs_since_last` | t - last_ts, capped at 30 days | velocity |
| 15 | `amount_zscore` | (amount - mean) / max(std, 0.25·mean, 50), over prior approved transactions, clipped to [-10, 50] | velocity, ATO |
| 16 | `amount_to_mean` | amount / mean, clipped to 100 | ATO, card-testing cash-out |
| 17 | `hour_unusualness` | 1 - (c_b + 1) / (n + 6), where c_b = prior transactions in the same 4-hour bucket and n = all prior transactions | ATO |
| 18 | `account_age_days` | (t - created_at) in days, from `accounts.parquet` | ring |
| 19 | `acct_history_cnt` | number of prior transactions | cold start |
| **D. Novelty and location** | | | |
| 20 | `new_device` | device never seen on this account | ATO, velocity, card testing |
| 21 | `acct_devices_30d` | distinct devices the account used in [t-30d, t) | ATO |
| 22 | `new_merchant` | merchant never seen on this account | velocity, ATO |
| 23 | `new_city` | city never seen (home city counts as seen from creation) | ATO |
| 24 | `km_from_home` | haversine distance from event location to home | ATO |
| 25 | `geo_speed_kmh` | haversine(previous event location, current) / max(Δt, 1 minute), capped at 5,000 | ATO |
| **E. Entity fan-out (real-time)** | | | |
| 26 | `dev_accts_1h` | distinct accounts seen on this device in [t-1h, t) | card testing |
| 27 | `dev_accts_30d` | ... in [t-30d, t) | ring |
| 28 | `ip_accts_1h` | distinct accounts seen on this IP in [t-1h, t) | card testing |
| 29 | `ip_accts_30d` | ... in [t-30d, t) | ring |
| 30 | `mer_accts_30d` | merchant's distinct customers in [t-30d, t) | ring (colluding merchants are small) |
| **F. Graph snapshot (warm, §6)** | | | |
| 31 | `graph_degree` | weighted degree in the account-to-account graph | ring |
| 32 | `graph_clustering` | clustering coefficient in the account-to-account graph | ring |
| 33 | `community_size` | size of the account's Louvain community | ring |
| 34 | `community_shared_devices` | devices shared by ≥ 2 members of the community | ring |
| 35 | `community_young_share` | share of community members younger than 30 days at T | ring |
| 36 | `ppr_risk` | personalized PageRank × number of graph nodes, seeded from labels available before T | ring (device reuse) |

- XGBoost uses all 36 features.
- Isolation Forest uses the 35 numeric ones.
- The final count may shrink after the ablation (§7.9), but should stay ≥ 30.

**How the fan-out features work.** Each entity keeps "last time each account used it", so the number of distinct accounts in a window equals the number of accounts whose last use falls inside that window. This works because events are processed in time order.

### 5.3 State model

```python
@dataclass
class AccountState:
    recent: list[
        tuple[int, float, str, int]
    ]  # (ts_ms, amount, merchant_id, declined) within the last 24 h, time-ordered
    last_ts: int | None
    last_lat: float | None
    last_lon: float | None
    n_all: int  # prior transactions, any status
    n_ok: int  # prior approved transactions
    sum_ok: float
    sumsq_ok: float  # variance = max((sumsq - sum^2 / n) / (n - 1), 0)
    hour_buckets: list[int]  # 6 counters (4-hour buckets)
    devices: dict[str, int]  # device_id -> last_ts_ms (entries older than 90 days pruned)
    merchants: set[str]
    cities: set[str]  # seeded with the home city
```

**Serialization.** JSON via `json.dumps(obj, sort_keys=True, separators=(",", ":"))`.

- Sets become sorted lists.
- Python writes floats with `repr`, so round trips are exact. A test asserts this.

**Entity indexes:**

- `InMemoryStore`: `last: dict[account_id, ts]` plus a `SortedList[(ts, account_id)]`, so a windowed count is a bisect.
- `RedisStore`: one sorted set per entity. Update with `ZADD ... GT` (only moves forward). Count with `ZCOUNT key <t0> (<t>` (exclusive upper bound). Occasionally trim with `ZREMRANGEBYSCORE` to 30 days.

### 5.4 Engine and stores

```python
class StateStore(Protocol):
    def committed(self, txn_id: str) -> dict | None:
        ...
        # Stored feature record, or None. Used for idempotency.

    def load(self, event: TransactionEvent) -> StateView:
        ...
        # Account state plus entity counts, as they were before this event.

    def commit(self, event: TransactionEvent, account: AccountState, record: dict) -> None:
        ...
        # Atomic: store the feature record, the new account state and the entity "touches".


class FeatureEngine:
    def __init__(self, store: StateStore, accounts: AccountDirectory, cfg: FeatureConfig): ...

    def process(
        self, event: TransactionEvent, *, commit: bool = True, extra: dict | None = None
    ) -> dict:
        if commit and (done := self.store.committed(event.txn_id)) is not None:
            return done  # redelivery: same answer, no state change
        view = self.store.load(event)
        feats = compute_features(event, view, self.accounts, self.cfg)  # pure function
        if commit:
            record = {**feats, **(extra or {})}
            self.store.commit(event, update_account(view.account, event, self.cfg), record)
            return record
        return feats
```

**`RedisStore`, per event:**

- **Load:** one pipeline containing `GET feat:{txn}`, `GET state:acct:{id}`, and the `ZCOUNT` calls for the 5 fan-out features.
- **Commit:** one `MULTI/EXEC` containing:
  - `SET feat:{txn} <json> EX 172800`
  - `SET state:acct:{id} <json>`
  - `ZADD ent:dev:{d} GT <ts> <acct>`, and the same for `ent:ip` and `ent:mer`
  - a periodic `ZREMRANGEBYSCORE`

**Why this is safe.** One scorer processes events in order. The `committed()` check before the transaction is therefore enough: a redelivered event returns its stored record and leaves state untouched.

If you ever run several consumers that could process the same message concurrently (a slow consumer plus an `XAUTOCLAIM`), move the check and the commit into one Lua script. Say this in interviews; don't build it (§9.7).

**`extra` carries `graph_snapshot_ts`.** The scorer picks the applicable snapshot *before* committing, so a redelivered event reuses the same snapshot and scores identically.

**Account reference data.** `AccountDirectory` loads `accounts.parquet` read-only (creation date, home city and coordinates). This stands in for a customer-profile lookup. It is not leakage: an account's creation date and home are known once it exists.

### 5.5 Offline replay and checkpoint

`python -m fraud.features.replay`:

1. Reads `events.parquet` and `accounts.parquet`.
2. Runs every event through `FeatureEngine(InMemoryStore)` in order.
3. Writes `data/features/hot_features.parquet` with these columns:
   - `txn_id`, `event_time`, `account_id`
   - `split` and `in_burn_in`
   - `feature_spec_version`
   - the 30 hot features
4. Just before the first event with `event_time >= test_start`, dumps the store to `data/state/checkpoint_<test_start>.json.gz`. This is the backfill source.

**Checks:**

- The output row count equals the event count.
- There are no NaNs.
- Feature ranges are written to `reports/feature_report.md`.

**Runtime target:** under ~3 minutes. If it is slower, profile with `cProfile` before optimising anything.

---

## 6. Graph layer

### 6.1 Account-to-account graph

For snapshot time T:

- **Input:** events with `event_time` in `[T - 30d, T)`.
- **Entities:** `device_id` and `ip`. **Merchants are excluded.**
- **Fan-out caps:** an entity is used only if it links between 2 and `cap` accounts in the window.
  - Choose the caps **from the training-period data**: the 99.5th percentile of accounts per device (and per IP) over 30-day windows, rounded.
  - Record the chosen values in `configs/graph.yaml` and in the report. The placeholders are `device_cap: 16`, `ip_cap: 18`.
  - The caps drop carrier NAT and VPN IPs, big offices, and card-testing devices. Without the cap, a card-testing device would fuse its unrelated victims into a fake "ring", and personalized PageRank would then push risk onto the victims' later, legitimate purchases.
- **Edge weight:** the number of capped entities the two accounts share.

```sql
-- $T, $lookback_days, $device_cap, $ip_cap are bound parameters
WITH win AS (
  SELECT account_id, device_id, ip
  FROM events
  WHERE event_time >= $T - to_days($lookback_days) AND event_time < $T
),
ad AS (SELECT DISTINCT account_id, device_id AS ent FROM win),
dev_ok AS (SELECT ent FROM ad GROUP BY ent HAVING COUNT(*) BETWEEN 2 AND $device_cap),
ai AS (SELECT DISTINCT account_id, ip AS ent FROM win),
ip_ok AS (SELECT ent FROM ai GROUP BY ent HAVING COUNT(*) BETWEEN 2 AND $ip_cap),
pairs AS (
  SELECT a.account_id AS u, b.account_id AS v
  FROM ad a JOIN ad b ON a.ent = b.ent JOIN dev_ok d ON d.ent = a.ent
  WHERE a.account_id < b.account_id
  UNION ALL
  SELECT a.account_id, b.account_id
  FROM ai a JOIN ai b ON a.ent = b.ent JOIN ip_ok i ON i.ent = a.ent
  WHERE a.account_id < b.account_id
)
SELECT u, v, COUNT(*) AS weight
FROM pairs
GROUP BY u, v
ORDER BY u, v;   -- deterministic order matters for Louvain (§6.6)
```

Split of work: **SQL builds the graph; NetworkX runs the algorithms.** Each tool does what it's good at.

### 6.2 Snapshot schedule and label delay

- **Offline calendar:** every day at 00:00 IST from 2026-01-02 to 2026-03-31. Events on 2026-01-01 get defaults.
- **Seeds for snapshot T:** accounts with any fraud label where `label_available_at < T`. This rule applies in every split, exactly as it would in production.
- **Young account:** `created_at > T - 30d`.

### 6.3 Algorithms and features

```python
G = nx.Graph()
G.add_weighted_edges_from(edges)  # sorted rows from §6.1
deg = dict(G.degree(weight="weight"))
clust = nx.clustering(G)  # unweighted; non-zero now, because the projected graph has triangles
comms = nx.community.louvain_communities(G, weight="weight", seed=42)
seeds = sorted(s for s in seed_accounts if s in G)
ppr = (
    nx.pagerank(G, alpha=0.85, personalization={s: 1.0 for s in seeds}, weight="weight")
    if seeds
    else {}
)
```

**Per-account outputs:**

- `graph_degree`
- `graph_clustering`
- `community_size`
- `community_shared_devices` (distinct capped devices linking ≥ 2 members)
- `community_young_share`
- `ppr_risk = ppr.get(node, 0) * G.number_of_nodes()`

**What is not a feature:**

- Community IDs are never features.
- A "known-fraud share of the community" is left out, because `ppr_risk` carries the same label information in a smoother form. Add it only if the ablation shows a gap.

**Defaults for accounts not in G:**

| Feature | Default |
|---|---|
| `graph_degree` | 0 |
| `graph_clustering` | 0 |
| `community_size` | 1 |
| `community_shared_devices` | 0 |
| `community_young_share` | 0 |
| `ppr_risk` | 0 |

**Outputs per snapshot:**

- `data/graph/offline/snapshot_ts=<T>/part.parquet`: rows only for accounts in G.
- `calendar.parquet`: all snapshot times.

### 6.4 Point-in-time join (two steps)

A single `ASOF JOIN` keyed on `account_id` would be **wrong**. It returns the latest snapshot *in which that account appeared*. If the account dropped out of the graph later (its shared edges aged out of the lookback), you would silently get stale values instead of defaults.

The correct join has two steps:

```sql
-- 1) Which snapshot applies to each event
CREATE TEMP TABLE ev_snap AS
SELECT f.txn_id, s.snapshot_ts
FROM hot_features f
ASOF LEFT JOIN calendar s ON f.event_time >= s.snapshot_ts;

-- 2) That snapshot's values for the account, or defaults if the account is absent
SELECT f.*,
       e.snapshot_ts                               AS graph_snapshot_ts,
       COALESCE(g.graph_degree, 0)                 AS graph_degree,
       COALESCE(g.graph_clustering, 0)             AS graph_clustering,
       COALESCE(g.community_size, 1)               AS community_size,
       COALESCE(g.community_shared_devices, 0)     AS community_shared_devices,
       COALESCE(g.community_young_share, 0)        AS community_young_share,
       COALESCE(g.ppr_risk, 0)                     AS ppr_risk
FROM hot_features f
JOIN ev_snap e USING (txn_id)
LEFT JOIN graph_features g
       ON g.account_id = f.account_id AND g.snapshot_ts = e.snapshot_ts;
```

Snapshot T contains only events *strictly before* T, so an event at exactly T may use snapshot T. The same rule applies online.

Tests cover three edge cases:

- an event exactly at T;
- an event before the first snapshot;
- an account missing from the applicable snapshot but present in an older one.

Labels are then joined in to produce `data/features/training_table.parquet`.

### 6.5 Live refresh protocol

**Inputs of the `graph-refresh` service:**

- `events.parquet` **filtered to `event_time < test_start`**. It must never read test-window rows from the simulator file; a test enforces this.
- The scorer's `data/scored/` output, for events at or after `test_start`.
- `labels.parquet`, but only rows whose `label_available_at` is before T (this stands in for a chargeback feed).

**Loop (every second):**

```
wm = GET sink:watermark
T  = (latest published snapshot) + 1 day
if wm >= T:
    compute snapshot(T) from inputs with event_time < T
    write data/graph/live/snapshot_ts=T/part.parquet   (temp file, then rename)
    pipeline: HSET graph:{T}:acct:{id} ...; EXPIRE ...
    ZADD graph:published T T
    log duration and T
```

**Why `wm >= T` is enough.** One scorer processes events in time order and flushes in that order. Once an event at or after T is flushed, every event before T is already flushed.

**Scorer lookup.** Once per micro-batch, read `graph:published` (about 20 entries). For each event, pick the latest T ≤ `event_time` by bisection, then run `HGETALL graph:{T}:acct:{account}` in a pipeline. Missing keys mean defaults.

**Staleness.** The live system can use snapshot T-1 while snapshot T is still being computed. To measure this:

- every scored row stores `graph_snapshot_ts`;
- the benchmark reports p95 staleness in hours, and the share of events that used an older snapshot than the offline join would have;
- a slower replay speed drives this toward 0.

**Graph parity check.** For sampled boundaries T, the live and offline snapshots must be equal (same accounts; values within 1e-9).

### 6.6 Determinism and cost

**Determinism:**

- Edges come out of SQL sorted.
- Louvain is seeded.
- Seeds are sorted.

Without the sorting, Louvain can return different communities on identical data, and the graph parity check fails.

**Cost:**

- The graph contains only accounts linked through capped entities (families, small offices, rings), so expect thousands of nodes, not tens of thousands.
- Each snapshot should take seconds.
- All 89 offline snapshots should take a few minutes; measure this.
- If a full run exceeds ~10 minutes on the VM, switch to weekly snapshots and document the change (cut list item 5).

---

## 7. ML pipeline

### 7.1 Leakage checklist (each item has a test)

| # | Rule | Enforced by |
|---|---|---|
| L1 | Features at time t use only events before t | Engine design (§5.1); `test_features_windows.py` |
| L2 | Graph features at t use snapshot T ≤ t, and T is built only from events before T | §6.4; `test_graph_leakage.py`, `test_asof_join.py` |
| L3 | Label-based graph features use only labels with `label_available_at < T` | §6.2; `test_graph_leakage.py` |
| L4 | No label column appears in `events.parquet` or in the stream | `test_sim_schema.py`, `test_replayer.py` |
| L5 | Splits are time-based; burn-in rows are excluded; splits don't overlap | `test_splits.py` |
| L6 | Everything that is fitted (Isolation Forest, anomaly quantiles, feature stats, weight, thresholds) uses only train or valid data | `test_training_smoke.py` checks which splits reach which fit |
| L7 | Categorical codes come from config, not from the data | `test_features_spec.py` |
| L8 | Attack and ring identifiers never become features | Feature list is fixed in `spec.py`; `test_features_spec.py` |
| L9 | The generator is frozen before modelling | `sim_config_sha256` in model metadata matches the tagged `sim-v1` |
| L10 | The test split is read only by `evaluate_test.py` | §7.11 |

### 7.2 Preprocessing and class imbalance

**Preprocessing is deliberately minimal.**

- **XGBoost input:** a pandas DataFrame with columns in `FEATURE_NAMES` order. `merchant_category` uses `pd.CategoricalDtype(categories=CATEGORY_LEVELS)` from config, with `enable_categorical=True`. An unknown category at inference becomes missing, which XGBoost handles natively.
- **Isolation Forest input:** the 35 numeric features. The engine's defaults mean there are no NaNs; assert this rather than imputing.
- **No scaling:** both models are tree-based.
- **No outlier removal:** outliers are the signal.
- **Duplicates:** `txn_id` uniqueness is asserted at load.
- **Missing values:** none by construction. A NaN anywhere is a bug and should fail loudly.

**Class imbalance.**

- Train without reweighting (`scale_pos_weight = 1`) and choose the operating point with thresholds (§7.7).
- Reason: the XGBoost probability is blended with a percentile. Reweighting would distort that probability and make the blend weight meaningless.
- Alternatives and why they were rejected are in §3.8.

### 7.3 Rules baseline (E1)

| Rule | Condition | Pattern |
|---|---|---|
| R1 | `acct_cnt_5m >= 5` | velocity |
| R2 | `geo_speed_kmh >= 900 and new_device == 1` | ATO |
| R3 | `dev_accts_1h >= 5 or (acct_small_1h >= 3 and acct_declines_1h >= 2)` | card testing |
| R4 | `dev_accts_30d >= 3 and account_age_days < 30` | ring |

- A transaction is flagged if any rule fires.
- Thresholds live in `configs/model.yaml`. You may tune them with a small grid on **train** only; record the chosen values.
- Report precision, recall, F1, alert rate and per-pattern recall.
- This is the reference point for "how much does ML add?"

### 7.4 XGBoost (E2, E3)

```python
XGBClassifier(
    objective="binary:logistic", tree_method="hist", enable_categorical=True,
    eval_metric="aucpr", n_estimators=2000, early_stopping_rounds=100,
    n_jobs=<vCPUs>, random_state=42, **params,
)
```

**Light random search: 20 configurations.**

| Parameter | Values |
|---|---|
| `max_depth` | {4, 6, 8} |
| `learning_rate` | {0.03, 0.05, 0.1} |
| `min_child_weight` | {1, 5, 10} |
| `subsample` | {0.7, 0.9} |
| `colsample_bytree` | {0.6, 0.9} |
| `reg_lambda` | {1, 5} |

**Procedure:**

1. Fit on `train`, with early stopping on `early_stop`.
2. Select by `valid` PR-AUC. Break ties by choosing fewer trees.
3. Log the actual time; budget ~10 minutes on the VM.
4. Save everything to `reports/experiments/xgb_search.json`.

**One parameter set for comparisons.**

- Phase 3 runs the search on the 30 hot features (E2) and produces parameter set P.
- Phase 4 trains E3 (36 features) with the same P. This keeps the graph ablation fair.
- You may re-run the search on 36 features afterwards. If you do, re-run E2 with the new parameters so both sides still match.

### 7.5 Isolation Forest (label-free)

```python
IsolationForest(
    n_estimators=200, max_samples=512, contamination="auto", random_state=42, n_jobs=-1
).fit(X_train_numeric)  # all train rows, labels ignored
```

**Anomaly score.**

- Raw score: `s = -score_samples(X)`, so higher means more anomalous.
- Percentile: store 1,001 quantiles of `s` over the train rows, then compute `anomaly_pct = np.interp(s, q, np.linspace(0, 1, 1001))`.
- Min-max scaling is not used, because a single extreme value would squash every other score.

**Anomaly reason.**

- Store per-feature train medians and IQRs, with a small floor on the IQR.
- For alerts, name the two features with the largest `|x - median| / IQR`.

**Why label-free.**

- The detector must not depend on the labels it is meant to complement.
- It is also identical across the leave-one-pattern-out runs (§7.6), which keeps that experiment fair.

### 7.6 Blend weight chosen by leave-one-pattern-out (LOPO) validation

`risk = w * p_xgb + (1 - w) * anomaly_pct`, with `w` from the grid {1.0, 0.95, 0.9, 0.85, 0.8, 0.7, 0.6, 0.5}.

**Procedure:**

1. For each pattern k in {VELOCITY, ATO, CARD_TESTING, RING}:
   - Train `XGB_-k` with parameters P on train rows **excluding pattern k**. Early stopping also excludes k.
   - For each `w`, compute `R_k(w)`: overall recall on `valid` (all rows, pattern k included) at the capacity-constrained threshold from §7.7. The threshold is recomputed for each (k, w).
2. Choose `w* = argmax_w mean_k R_k(w)`. On ties, take the larger `w`.
3. The final model (E4) is `XGB_full` (all patterns) plus Isolation Forest at `w*`.

**Why this is legitimate.** Isolation Forest exists to catch patterns without labels. Choosing its weight on simulated "unseen pattern" scenarios optimises for exactly that job, and the test split stays untouched.

**If `w* = 1.0` (Isolation Forest never helps):**

1. Say so, then try the fallback from §3.8: a two-queue union that reserves 10% of the alert budget for anomaly-only alerts, evaluated with the same LOPO protocol.
2. Keep whichever design is better.
3. If neither helps, report that honestly in the README and drop "ensemble" from the CV. An honest negative result beats a claim you can't defend.

### 7.7 Decision policy

- **`review_budget = 0.02`:** alerts may be at most 2% of transactions. This represents analyst capacity and lives in config.
- **REVIEW threshold `t_r`:** on `valid`, the threshold that maximises F1 while the alert rate stays within `review_budget`.
- **HOLD threshold `t_h`:** on `valid`, the lowest threshold with precision ≥ 0.95, requiring at least 50 alerts above it. If no threshold qualifies, HOLD is disabled and the README says so.
- **Decision:** `HOLD` if `risk >= t_h`, otherwise `REVIEW` if `risk >= t_r`, otherwise `ALLOW`.
- **"At the operating point"** means alerts = REVIEW ∪ HOLD.
- **HOLD action:**
  - The scorer sets `hold:acct:{id}` with a 7-day TTL.
  - Later events on that account are annotated `account_on_hold = true` in the output.
  - The annotation is **output only**; it is never a model input.

### 7.8 Metrics

| Group | Metrics |
|---|---|
| Threshold-free | PR-AUC (average precision; the headline metric); ROC-AUC (secondary) |
| At the operating point | Precision, recall, F1, false-positive rate (false alerts / legitimate transactions), alert rate, value detection rate (flagged fraud amount / total fraud amount), recall per pattern |
| HOLD tier | Precision, count |
| Online (§9.6) | Throughput, latency p50/p95/p99, graph staleness, DLQ count, re-score match rate |

**Why not accuracy.** At ~1.5% prevalence, "always legitimate" is 98.5% accurate. What matters is how much fraud is caught within the analyst capacity, and how precise HOLD is. Be ready to explain this in interviews.

**Known limitation (write it in the README).**

- There is a single simulated dataset and no confidence intervals.
- Rows within one attack are correlated, so a naive row-level bootstrap would overstate certainty.

### 7.9 Experiments

| ID | What | Why it matters |
|---|---|---|
| E1 | Rules R1-R4 | Reference point |
| E2 | XGBoost, 30 hot features | Behavioural model alone |
| E3 | XGBoost, 36 features | **Graph ablation:** E3 vs E2 ring recall shows whether the differentiator works |
| E4 | E3 + Isolation Forest at `w*` | Final model |
| E5 | LOPO: for each pattern, recall on that pattern with `XGB_-k` alone vs blended at `w*`, plus the change in overall precision | Evidence for the ensemble |
| E6 | Live replay of the test window | Online correctness and performance (§9.6) |

During development, evaluate on `valid` only. The test split is evaluated once, at the end (§7.11).

**Results tables.** `reports/results.md` is generated, not hand-written.

| Exp | PR-AUC | Precision | Recall | F1 | FPR | Alert rate | VEL | ATO | CT | RING |
|---|---|---|---|---|---|---|---|---|---|---|

| Withheld pattern | Recall on it: `XGB_-k` alone | Recall on it: blended at `w*` | Overall precision: alone → blended |
|---|---|---|---|

### 7.10 Explanations

**Per-transaction contributions.**

```python
contribs = booster.predict(xgb.DMatrix(X, enable_categorical=True), pred_contribs=True)
# shape (n, n_features + 1); the last column is the bias; each row sums to the log-odds margin
```

**Reason codes.**

- For REVIEW and HOLD rows, take the top three **positive** contributions and fill templates from `configs/reason_codes.yaml`, for example:

  | Feature | Template |
  |---|---|
  | `acct_cnt_5m` | "{value:.0f} payments in the previous 5 minutes" |
  | `geo_speed_kmh` | "Implied travel speed of {value:,.0f} km/h since the last payment" |
  | `dev_accts_1h` | "Device used by {value:.0f} accounts in the last hour" |
  | `ppr_risk` | "Closely linked to accounts with confirmed fraud" |
  | `community_young_share` | "{value:.0%} of linked accounts are under 30 days old" |

- **Anomaly reason:** if `anomaly_pct >= 0.99`, add "Unusual combination: {f1} and {f2} far outside typical values" (§7.5).

**Checks.**

- Confirm that `pred_contribs` works with the categorical column on your XGBoost version. If it does not, one-hot encode `merchant_category` (10 columns) and bump `FEATURE_SPEC_VERSION`.
- A test asserts that contributions sum to the model margin within 1e-4. This proves the explanation path matches the model.

**Offline global view.**

- Draw a `shap` beeswarm on a 5K-row validation sample (analysis dependency group).
- If `shap` has version friction with your XGBoost, plot mean |contribution| per feature with matplotlib instead.
- Save the figure in `reports/figures/`.

### 7.11 Test-set discipline

- `fraud.modeling.splits.load("test")` raises unless the environment variable `ALLOW_TEST=1` is set. Only `make evaluate-test` sets it.
- Each run appends a line to `reports/test_runs.log`: timestamp, model version, git commit.
- The README reports the **first** test run of the final model version. If anything changes afterwards, bump the model version and say so.

---

## 8. Model versioning without MLflow

```
models/
  v1/
    xgb.ubj                  # XGBoost native binary JSON
    iforest.joblib           # scikit-learn pickle (version-sensitive)
    anomaly_quantiles.npy
    feature_stats.json       # medians and IQRs for anomaly reasons
    reason_codes.yaml        # the templates used at training time
    metadata.json
  CURRENT                    # text file containing "v1"
```

**`metadata.json` fields:**

- `model_version`, `created_at`, `git_commit`, `plan_version`
- `feature_spec_version`, `feature_names` (ordered), `categorical_levels`
- `sim_config_sha256`, `generator_version`
- `splits` (boundaries), `graph_config` (caps, lookback, label delay)
- `xgb_params`, `best_iteration`, `iforest_params`
- `blend_weight`, `thresholds` (`review`, `hold`), `review_budget`
- `metrics` (`valid`, plus `test` filled in by `evaluate_test.py`)
- `library_versions` (python, numpy, pandas, xgboost, scikit-learn, networkx, duckdb)

**Loading rules (in `RiskModel`):**

- Load the version named by the `MODEL_VERSION` environment variable; otherwise use `CURRENT`.
- Refuse to start if `feature_spec_version` differs from the code.
- Refuse to start if the xgboost or scikit-learn major.minor versions differ from the metadata. Pickles are version-sensitive.
- The scorer also refuses to start if Redis `meta:state_version` differs from the model's `feature_spec_version`. This forces a fresh backfill.
- Every scored row records `model_version`, `feature_spec_version` and `graph_snapshot_ts`.

**Changing models:**

1. Train v2 into `models/v2/`.
2. Update `CURRENT`.
3. Run `docker compose restart scorer api`.

There is no hot reload. Say why in interviews: one model, no promotion workflow, and restarts are cheap.

**Security note:** joblib/pickle files can execute code when loaded. Only load artifacts you built yourself.

**Interview line:** "In the Delivery Delay project I used MLflow's registry for promotion. Here there's one model and no promotion flow, so a versioned folder with metadata and startup compatibility checks is the honest minimum."

---

## 9. Streaming pipeline

### 9.1 Replayer (producer)

**Command:**

```
python -m fraud.stream.replayer (--speedup 3600 | --rate 500 | --max) [--limit N] [--start-at ISO]
```

**Behaviour:**

- **Input.** Reads the test-window events from `events.parquet`, selecting only the §3.5 columns. It asserts that no label column is present and that events are sorted.
- **Pacing.**
  - `--speedup S`: event i is sent at `wall0 + (t_i - t_0) / S`.
  - `--rate R`: a fixed R events/s.
  - `--max`: no sleeping (used for the capacity benchmark).
- **Batching.** Events are sent in pipelines of 100.
- **Message fields.** Adds `ingest_ts` (epoch ms at send time) and `schema_version = 1`, then writes with `XADD txn:events MAXLEN ~ 500000 * field value ...`.
- **Backpressure.**
  - Every 1,000 events, the replayer reads `XINFO GROUPS txn:events` (the `lag` field needs Redis ≥ 7.0).
  - If group `scorers` lags by more than 20,000 entries, it pauses until the lag drops below 5,000. The two thresholds (hysteresis) prevent flapping.
- **Logging.** Logs events sent, the simulated clock and every pause.

### 9.2 Scorer service (consumer)

```
startup
  RiskModel.load()            # compatibility checks, §8
  check meta:state_version    # refuse to start on mismatch
  AccountDirectory.load()
  me = CONSUMER_NAME or hostname
  drain own pending: XREADGROUP GROUP scorers <me> COUNT 200 STREAMS txn:events 0   (repeat until empty)

loop
  every 30 s (reclaim):
    XPENDING txn:events scorers IDLE 60000 - + 100      # extended form: id, owner, idle ms, delivery count
    delivery count > 3  -> XRANGE the entry, XADD txn:dlq (with error), XACK
    otherwise           -> XCLAIM to <me>, process like any other message

  msgs = XREADGROUP GROUP scorers <me> COUNT 200 BLOCK 1000 STREAMS txn:events >
  published = ZRANGE graph:published 0 -1              # once per batch; about 20 entries

  for msg in msgs (stream order):
      event = TransactionEvent.model_validate(fields)  # invalid -> XADD txn:dlq, XACK, continue
      T = latest published snapshot <= event.event_time   (bisect)
      record = engine.process(event, extra={"graph_snapshot_ts": T})   # atomic commit; idempotent
      batch.append((msg.id, event, record))

  graph   = pipeline HGETALL graph:{T}:acct:{account} for the batch   # missing -> defaults (§6.3)
  results = risk_model.score_batch(features)           # vectorised: XGBoost + IsolationForest + blend + decision
  reasons for REVIEW/HOLD rows only (pred_contribs on those rows)
  HOLD rows -> SET hold:acct:{id} 1 EX 604800 ; rows whose account is already held -> account_on_hold = true
  sink.append(rows, msg_ids)

  if sink.should_flush():                              # >= 2,000 rows or 2 s since the last flush
      sink.flush()                                     # Parquet, atomic rename
      XACK only the flushed ids                        # ack AFTER the data is durable
      SET sink:watermark <max event_time flushed>
      LPUSH alerts:recent ... ; LTRIM alerts:recent 0 499
      HINCRBY metrics:scorer ... ; HSET heartbeat ; LPUSH/LTRIM metrics:latency_ms

on SIGTERM: stop reading, flush, ack, exit 0           # docker stop sends SIGTERM; stop_grace_period covers the flush
```

**Scored row schema.** Each row written to `data/scored/` contains:

- the event fields (§3.5)
- all 36 features and `graph_snapshot_ts`
- `p_xgb`, `anomaly_pct`, `risk`, `decision`
- `reasons` (a JSON string) and `account_on_hold`
- `model_version`, `feature_spec_version`
- `ingest_ts`, `scored_ts`, `consumer`, `stream_id`

Features are stored on purpose. They make the re-score check (§9.6) and on-demand explanations possible.

### 9.3 Idempotency and failure matrix

**Delivery model.** Delivery is at-least-once. The effect on state is exactly-once: every state change happens inside the per-event commit, and a redelivered event returns its stored record without touching state. The output is deduplicated by readers.

| Crash point | Redis state | Parquet output | On restart |
|---|---|---|---|
| Before the feature commit | Unchanged | None | Message is still pending, so it is processed normally |
| After the commit, before the flush | Updated once | None | Pending message returns the stored record (same features, same snapshot), so the row is written once |
| After the flush, before `XACK` | Updated once | Row written | Pending message returns the stored record, so an identical row is written again; readers deduplicate on `txn_id` |
| Poison message | Unchanged | DLQ entry | Acknowledged after the DLQ write |
| Graph job crash mid-snapshot | Partial hash writes for T | `.tmp` file (ignored) | T is not in `graph:published`, so the job recomputes it; writes are idempotent (same values) |
| Redis restart (AOF `everysec`) | Up to ~1 s of writes may be lost | Unaffected | **Known limitation.** A lost stream tail means those events are never scored, and a few already-written rows may refer to state Redis no longer has. The production answer is a replicated log (Kafka) and replicated state. Put this in the README. |

`tests/test_scorer_recovery.py` injects failures at each point through a `crash_after=` hook and checks the outcome.

### 9.4 Sink and readers

**Sink (writer).**

- Buffers rows and writes `data/scored/date=<event date>/part-<consumer>-<seq:06d>.parquet.tmp`, then renames it with `os.replace`. The rename is atomic on the same filesystem, so readers never see a partial file.
- A flush that spans two dates writes one file per date.
- **Watermark:** the largest `event_time` in the flushed batch. This is valid because processing is ordered.

**Readers.** `fraud.storage.duck.connect()` opens an in-memory DuckDB connection (no database file, so no lock) and defines this view:

```sql
CREATE OR REPLACE VIEW scored AS
SELECT *
FROM read_parquet('data/scored/*/*.parquet', hive_partitioning = true, union_by_name = true)
QUALIFY row_number() OVER (PARTITION BY txn_id ORDER BY scored_ts) = 1;
```

**Monitoring queries.** These go in the README and are used by the dashboard.

```sql
-- Riskiest merchants
SELECT merchant_id, COUNT(*) AS txns, AVG(risk) AS avg_risk,
       SUM(CASE WHEN decision <> 'ALLOW' THEN 1 ELSE 0 END) AS alerts
FROM scored GROUP BY merchant_id ORDER BY alerts DESC LIMIT 10;

-- Alert rate per simulated hour
SELECT date_trunc('hour', event_time) AS hour,
       AVG(CASE WHEN decision <> 'ALLOW' THEN 1.0 ELSE 0.0 END) AS alert_rate
FROM scored GROUP BY 1 ORDER BY 1;

-- End-to-end latency percentiles (ms)
SELECT quantile_cont(scored_ts - ingest_ts, [0.5, 0.95, 0.99]) AS latency_ms FROM scored;
```

**Small files.** Slow replays produce many small files, which slows these queries down. Compaction is NICE TO HAVE (§2.2).

### 9.5 Backfill (one-shot service)

1. **Skip check.** If `backfill:done:{FEATURE_SPEC_VERSION}:{test_start}` exists, exit 0.
2. **Load state.** Read `data/state/checkpoint_<test_start>.json.gz`.
   - Account blobs go in with chunked `MSET`.
   - Entity sorted sets go in with chunked `ZADD`, trimmed to 30 days.
   - Writes are idempotent, so a partial earlier run is simply overwritten.
3. **Publish the first snapshot.** Take offline snapshot `T = test_start` (built from history only) and write `graph:{T}:acct:*`, then `ZADD graph:published`.
4. **Create the stream and group.** Run `XGROUP CREATE txn:events scorers 0 MKSTREAM` and ignore `BUSYGROUP` if the group already exists. Create it **before** replay starts, at ID `0`, so no early events are missed.
5. **Write markers.** Set `meta:state_version` and the skip-check marker.

`make demo-reset` wipes the Redis volume plus `data/scored/` and `data/graph/live/`, so the next `make up` backfills again.

### 9.6 Benchmark and correctness method (E6)

1. **Capacity.**
   - After a reset, replay the whole test window with `--max`.
   - Throughput = rows / (last `scored_ts` - first `scored_ts`).
   - Repeat with micro-batch sizes 1, 50, 200 and 500.
2. **Latency.**
   - Replay with `--rate` at about 50% and about 80% of measured capacity.
   - Report p50/p95/p99 of `scored_ts - ingest_ts`.
   - Latency measured under `--max` is mostly queueing time, so don't report it.
3. **Correctness (`scripts/rescore_check.py`).**
   - Re-score the live output offline, using the stored features and recorded snapshot ids. `p_xgb`, `risk` and `decision` must be identical (tolerance 1e-9).
   - Compare the live hot features with `hot_features.parquet` for the test window. They must be identical, because both start from the same state at `test_start`.
   - Check live vs offline graph parity for 3 sampled boundaries.
4. **Live vs offline metrics.**
   - Compute test metrics from the live output (joined with labels) and compare them with offline E4.
   - Any difference must be explained by graph staleness (§6.5); report both numbers and the staleness.
5. **Record the context.** Write to `reports/benchmark.md`: CPU model, VM vCPUs and RAM, Redis version, Python version, batch size, pacing mode, and the date.

### 9.7 What you would change at 100x scale (interview answers, not build items)

- **Log:** Kafka partitions keyed by `account_id`. Ordering holds per account, parallelism grows with partitions, and you get retention and replication.
- **State:** either shard the Redis state by account, or move to a stateful stream processor with checkpointed state (Flink or Kafka Streams).
- **Overlapping consumers:** if two consumers could process the same message at once, make the idempotency check and the commit one atomic Lua script.
- **Graph features:** compute simple entity-link counts in the stream; run heavy graph jobs on a cluster or a graph store; treat streaming community detection as research territory.
- **Features and serving:** a feature store to manage offline/online parity, and a separate synchronous authorization path (tens of milliseconds) that reads the same online features.
- **Labels and models:** a delayed-label (chargeback) pipeline that feeds retraining and drift monitoring.
- **Monitoring:** proper metrics and alerting on consumer lag, DLQ growth and latency SLOs.

**Partitioned-streams detail (NICE item 1).**

- Use streams `txn:events:{p}` with `p = crc32(account_id) % K`, and one scorer per partition.
- Each partition keeps its own watermark.
- The graph job must use the **minimum** watermark across partitions before publishing T. This is a good detail to mention in interviews.

---

## 10. FastAPI service

```
src/fraud/api/
  main.py        # app factory + lifespan (loads model, Redis client, engine once)
  deps.py        # dependency getters
  routes/
    health.py  score.py  alerts.py  transactions.py  metrics.py
src/fraud/schemas.py   # TransactionEvent, ScoreResponse, Reason, Alert, ... (shared with the scorer)
```

### Endpoints

| Method | Path | Purpose | Source | Errors |
|---|---|---|---|---|
| GET | `/health` | Liveness plus readiness details: Redis, model version, feature spec, latest snapshot, scorer heartbeat age | Redis `PING`, metadata, `graph:published`, `metrics:scorer` | 503 if Redis is down or the model isn't loaded |
| POST | `/score` | **What-if** scoring of a *new* event; **never changes state** | `FeatureEngine(RedisStore).process(event, commit=False)` + graph lookup + `RiskModel` | 422 on a bad body; 503 if Redis is down |
| GET | `/alerts?decision=&limit=` | Recent alerts | Redis `alerts:recent` | 422 on bad parameters |
| GET | `/transactions/{txn_id}` | Full scored record, features and reasons | DuckDB view over `data/scored/` | 404 if not found |
| GET | `/metrics` | Processed count, alerts, throughput over the last 10 s, stream lag, pending count, rolling p50/p95 latency, DLQ size | Redis metrics + `XINFO GROUPS` | 503 |

### Design choices

- **`/score` is read-only.** The stream is the single writer of state; API writes would double-count.
  - If `txn_id` was already processed, `/score` returns the score computed from the stored record, with `already_processed: true`.
  - Scoring an *old* event against today's state would not be a point-in-time result, so that case is documented.
- **No batch endpoint.** Batch work happens in the stream through micro-batching. A batch HTTP endpoint would duplicate that path without teaching anything new.
- **Validation.**
  - `TransactionEvent` uses `ConfigDict(extra="forbid")`, so a request (or stream message) carrying `is_fraud` is rejected. This is leakage rule L4, enforced at the boundary.
  - Field constraints: `amount > 0`; latitude/longitude ranges; `channel` and `status` as `Literal`s; `ip` as `IPv4Address`; `schema_version: Literal[1]`.
  - `merchant_category` is validated against `categories.yaml`, and a test keeps the two in sync.
- **Model loading.** Happens once, in `lifespan`, through `RiskModel.load()`, so the API and scorer load versions the same way (§8). `/health` reports the loaded version.
- **Concurrency.** Endpoints are plain `def` functions using the sync redis-py client, so FastAPI runs them in its threadpool. Don't mix sync and async Redis clients. One uvicorn worker is enough.
- **Errors.** A handler maps `redis.exceptions.ConnectionError` to 503 with a JSON body. Validation errors use FastAPI's standard 422.

```python
class ScoreResponse(BaseModel):
    txn_id: str
    fraud_probability: float
    anomaly_percentile: float
    risk_score: float
    decision: Literal["ALLOW", "REVIEW", "HOLD"]
    reasons: list[Reason]  # feature, value, contribution, text
    model_version: str
    graph_snapshot_ts: datetime | None
    already_processed: bool
    state_updated: Literal[False] = False
```

---

## 11. Dashboard (minimal)

| Panel | Source | Refresh |
|---|---|---|
| System strip: events/s, stream lag, p95 latency, DLQ size, scorer heartbeat | `GET /metrics` | 2 s |
| Alert rate and HOLD count per simulated hour | DuckDB `scored` | 5 s |
| Replay scorecard: precision and recall so far, per pattern. **Labelled on screen as "evaluation data the scorer never sees".** | DuckDB `scored` joined with `labels.parquet` | 10 s |
| Recent alerts: time, account, merchant, amount, decision, risk, top reason | `GET /alerts` | 2 s |
| Top risky merchants and accounts | DuckDB | 10 s |
| Alert detail: `p_xgb`, anomaly percentile, `w`, final risk, thresholds, reasons, key feature values | `GET /transactions/{id}` | On select |
| *(NICE)* Ego-graph of the selected account | Snapshot Parquet + `st.graphviz_chart` | On select |

**Implementation notes.**

- Refresh each panel with `@st.fragment(run_every=...)`. If your Streamlit version lacks this, use `streamlit-autorefresh`.
- Cache the DuckDB connection with `st.cache_resource`. Never cache query results longer than the panel's refresh interval.
- No theming work.

---

## 12. Docker and Compose

### 12.1 Dockerfile (one image for every Python service)

```dockerfile
FROM python:3.12-slim

# uv from its official image; pin the tag to the uv version you use locally (`uv --version`)
COPY --from=ghcr.io/astral-sh/uv:<uv-version> /uv /uvx /bin/

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# 1) Dependencies first. This layer stays cached until pyproject.toml or uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 2) Then the code. Editing code does not reinstall dependencies.
COPY README.md ./
COPY src/ src/
COPY configs/ configs/
COPY dashboard/ dashboard/
RUN uv sync --frozen --no-dev

# 3) Don't run as root. Bind mounts must be writable by this uid (1000 = the default first Ubuntu user).
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app
```

- `--no-dev` installs only `[project].dependencies`. The `sim`, `analysis` and `dev` groups stay out of the image.
- Training happens in the host virtual environment. The image only serves.

### 12.2 `docker-compose.yml`

```yaml
x-app: &app
  build: .
  image: fraud-app:local
  env_file: .env                       # REDIS_URL=redis://redis:6379/0 inside Compose
  volumes:
    - ./data:/app/data
    - ./models:/app/models:ro

services:
  redis:
    image: redis:8-alpine              # replace with the exact tag you pulled; any Redis >= 7.0 works (Valkey is a drop-in alternative)
    command: ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec"]
    volumes: [redis-data:/data]
    ports: ["6379:6379"]               # host access for local runs and redis-marked tests
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10

  backfill:
    <<: *app
    command: ["python", "-m", "fraud.stream.backfill"]
    depends_on:
      redis: {condition: service_healthy}
    restart: "no"

  scorer:
    <<: *app
    command: ["python", "-m", "fraud.stream.scorer"]
    depends_on:
      backfill: {condition: service_completed_successfully}
    restart: unless-stopped
    stop_grace_period: 20s

  graph-refresh:
    <<: *app
    command: ["python", "-m", "fraud.graph.refresh_live"]
    depends_on:
      backfill: {condition: service_completed_successfully}
    restart: unless-stopped

  api:
    <<: *app
    command: ["uvicorn", "fraud.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
    ports: ["8000:8000"]
    depends_on:
      backfill: {condition: service_completed_successfully}
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 10s
      timeout: 3s
      retries: 10

  dashboard:
    <<: *app
    command: ["streamlit", "run", "dashboard/app.py", "--server.address=0.0.0.0", "--server.port=8501"]
    ports: ["8501:8501"]
    depends_on:
      api: {condition: service_healthy}

  replayer:
    <<: *app
    command: ["python", "-m", "fraud.stream.replayer", "--speedup", "3600"]
    profiles: ["demo"]
    depends_on:
      scorer: {condition: service_started}

volumes:
  redis-data: {}
```

**Notes:**

- There is no top-level `version:` key; Compose v2 no longer uses it.
- Every Python service shares the same image. Compose may start several builds, but the layer cache makes the repeats instant.

**Startup order:** redis → backfill (runs to completion) → scorer, graph-refresh, api → dashboard. The replayer runs only when you ask for it.

| Make target | Command |
|---|---|
| `make up` | `docker compose up -d --build` |
| `make demo` | `docker compose --profile demo up replayer` |
| `make logs` | `docker compose logs -f scorer graph-refresh` |
| `make demo-reset` | `docker compose down -v && rm -rf data/scored data/graph/live` |
| `make smoke` | Start the stack, replay 2,000 events with `--max --limit 2000`, then assert 2,000 unique scored rows and a healthy `/health` |

**Resources.**

- Give the VM 8-10 GB RAM and 6-8 vCPUs if the host allows (the i5-1235U has 12 threads).
- Watch `docker stats` during a replay and record peak memory in the benchmark report.

**Common mistakes:**

- **Bind mount permissions.** If `id -u` is not 1000, change the Dockerfile uid or set `user:` in Compose.
- **Missing data.** Running `make up` before `make all`: backfill needs the checkpoint and the offline snapshots.
- **Wrong Redis host.** `.env` pointing at `localhost` inside containers; it must be `redis`.
- **Stale image.** Forgetting `--build` after code changes.
- **No start conditions.** `depends_on` without `condition:`, so services start before Redis is ready.
- **Wrong filesystem.** Keeping the repo on a VMware shared folder instead of the VM's own disk.

---

## 13. Testing strategy (tests are written with each phase, not at the end)

**Shape of the test suite:**

- **Many fast unit tests:** pure feature functions, rules, metrics, graph algorithms on toy graphs, join edge cases.
- **Some integration tests** (need Redis): store parity, idempotency, scorer recovery, the API against Redis.
- **Two end-to-end checks:** the Compose smoke test and the re-score check.

**Markers and commands:**

| Command | What it runs |
|---|---|
| `make test` | `pytest -m "not redis and not slow"` (seconds; run after every change) |
| `make test-redis` | `pytest -m redis` against `REDIS_URL=redis://localhost:6379/15` (start Redis first with `make redis-up`) |
| `make test-all` | Everything |

**Fixtures (`tests/conftest.py`):**

- **`tiny_sim`** (session-scoped): runs the simulator with `configs/sim_tiny.yaml` into a temp directory. The config has 300 accounts, 6 days, one attack per pattern and one ring, so it finishes in seconds.
- **`redis_client`**: uses DB 15, runs `FLUSHDB` before and after each test, and is used only by `redis`-marked tests.
- **`make_events(...)`**: a builder for hand-written event sequences with exact timestamps.

**Coverage policy:** there is no blanket percentage gate. Instead, the rules are:

- every feature has at least one hand-computed test;
- every leakage rule (§7.1) has a test;
- every crash point (§9.3) has a test.

`pytest --cov` output is informational only.

| Test file | Type | Phase | What it proves |
|---|---|---|---|
| `test_smoke.py` | unit | 0 | Package imports; config loads |
| `test_sim_population.py` | unit | 1 | Population counts and shares match config (mid-simulation accounts, families, IP pools) |
| `test_sim_determinism.py` | unit | 1 | Same seed gives identical file hashes |
| `test_sim_schema.py` | unit | 1 | Event columns match §3.5, with no label columns (L4); `txn_id` unique; sorted; no event before account creation |
| `test_sim_patterns.py` | unit | 1 | Each pattern's signature exists (§4.8); ring constraints hold |
| `test_sim_hard_negatives.py` | unit | 1 | VPN "impossible travel", families, new legitimate accounts and micro-bursts exist |
| `test_features_spec.py` | unit | 2 | 36 names in a fixed order; category levels come from config (L7); no ID-like features (L8) |
| `test_features_windows.py` | unit | 2 | Half-open windows; current event excluded; cold-start defaults (L1) |
| `test_features_geo.py` | unit | 2 | Haversine Delhi-Mumbai ≈ 1,150 km (±15 km); speed cap; 1-minute Δt floor |
| `test_state_serialization.py` | unit | 2 | `AccountState` JSON round trip is byte-for-byte stable |
| `test_replay.py` | unit | 2 | Output row count = event count; no NaN; checkpoint written at the boundary |
| `test_splits.py` | unit | 3 | Boundaries from config; burn-in excluded; no overlap; test split blocked without `ALLOW_TEST` (L5, L10) |
| `test_metrics.py` | unit | 3 | Operating-point selection respects the budget; per-pattern recall on a toy example |
| `test_rules.py` | unit | 3 | R1-R4 fire on crafted rows |
| `test_graph_projection.py` | unit | 4 | Toy data: family and ring become edges; an over-cap IP produces none; weights correct; output sorted |
| `test_graph_algorithms.py` | unit | 4 | Clustering is 0 on the raw bipartite toy graph but > 0 on the account-to-account graph; defaults for absent accounts |
| `test_graph_leakage.py` | unit | 4 | Snapshot T has no edges at or after T; a label available at T+1s is not a seed (L2, L3) |
| `test_asof_join.py` | unit | 4 | Event exactly at T; event before the first snapshot; the stale-account trap returns defaults |
| `test_training_smoke.py` | unit, slow | 3-5 | Tiny data trains end to end; artifacts and full metadata written; fits see only train/valid (L6) |
| `test_scoring.py` | unit | 5 | `RiskModel` is deterministic; percentile mapping is monotonic; decision tiers correct |
| `test_reason_codes.py` | unit | 5 | Contributions sum to the margin (1e-4); every feature has a template |
| `test_parity_redis.py` | redis | 6 | In-memory and Redis stores give identical features on `tiny_sim` |
| `test_idempotency.py` | redis | 6 | Processing an event twice: same record, state bytes unchanged, sorted-set scores unchanged |
| `test_scorer_recovery.py` | redis | 6 | Each crash point in §9.3 behaves as documented; poison message goes to the DLQ |
| `test_sink.py` | unit | 6 | Atomic writes; `.tmp` files ignored; the deduplicating view returns one row per `txn_id` |
| `test_replayer.py` | redis | 6 | Messages contain exactly the schema fields; backpressure pauses when lag is high |
| `test_graph_live.py` | redis, slow | 6 | Live snapshot equals the offline snapshot for a boundary; history is never read at or after `test_start` |
| `test_api.py` | unit + redis | 7 | `/health` fields; body with `is_fraud` gives 422; `/score` leaves `state:acct:*` unchanged; 404 on an unknown transaction |
| `make smoke` | e2e | 9 | Compose stack scores 2,000 events once each; `/health` ok |
| `scripts/rescore_check.py` | e2e | 6, 9 | Live output reproduces offline (§9.6) |

---

## 14. CI (and why there is no CD)

**CI** (continuous integration) checks every push. **CD** (continuous delivery/deployment) would publish images and deploy them. This project has CI only:

- Delivery Delay already covers CD to GHCR.
- "Deployment" here means `docker compose up` on the VM plus a recorded demo, which is documented honestly.

`.github/workflows/ci.yml`:

```yaml
name: ci
on: [push, pull_request]

jobs:
  lint-unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5            # use the current major version
      - uses: astral-sh/setup-uv@v6          # check the action README for the current major version
        with:
          python-version: "3.12"
      - run: uv sync --frozen --all-groups
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run pytest -m "not redis and not slow"

  redis-integration:
    runs-on: ubuntu-latest
    services:
      redis:
        image: redis:8-alpine                # same tag as docker-compose.yml
        ports: ["6379:6379"]
        options: >-
          --health-cmd "redis-cli ping" --health-interval 5s
          --health-timeout 3s --health-retries 10
    env:
      REDIS_URL: redis://localhost:6379/15
    steps:
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v6
        with:
          python-version: "3.12"
      - run: uv sync --frozen --all-groups
      - run: uv run pytest -m redis

  image-build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - run: docker build -t fraud-app:ci .   # proves the Dockerfile builds; nothing is pushed
```

**What CI does not do:**

- It never needs `data/` or `models/`. Tests generate tiny fixtures.
- It does not run full training or the full replay. Those depend on your hardware and are run locally with `make all` and `make bench`.
- Check GitHub's current Actions billing if the repository is private.

---

## 15. Development environment

### 15.1 Required software

| Where | Software | Version | Why | Verify |
|---|---|---|---|---|
| Host | VMware Workstation, VS Code with Remote-SSH | Current | Your existing setup | Connect to the VM |
| VM | Ubuntu | 24.04 LTS | Development OS | `lsb_release -a` |
| VM | Python | 3.12.x (the 24.04 default) | Runtime | `python3 --version` |
| VM | uv | Latest at setup, then pinned in the Dockerfile | Environments and lockfile | `uv --version` |
| VM | Git + a GitHub SSH key | Any recent | Version control, CI | `git --version`; `ssh -T git@github.com` |
| VM | Docker Engine + Compose plugin | Current stable (Compose v2) | Redis and the full stack | `docker version`; `docker compose version`; `docker run --rm hello-world` |
| VM | Claude Code | Current | Implementation partner | `claude --version` |
| VM (optional) | `redis-tools` | Any | `redis-cli` on the VM itself | `redis-cli -p 6379 ping` (or `docker compose exec redis redis-cli ping`) |

**Not required:** PostgreSQL, MLflow, Kafka, Node.js (only needed if you install Claude Code through npm), CUDA, any cloud CLI, the DuckDB CLI (the Python package is enough).

### 15.2 Setup steps (inside the VM)

```bash
# Git
sudo apt update && sudo apt install -y git build-essential
git config --global user.name "<name>" && git config --global user.email "<email>"

# Docker Engine: follow Docker's official "Install Docker Engine on Ubuntu" guide (apt repository).
# Do not use the snap package or Docker Desktop. Then:
sudo usermod -aG docker "$USER"      # log out and back in afterwards
docker run --rm hello-world

# uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Claude Code (already installed in your setup; to reinstall, use the native installer from the official docs)
#   curl -fsSL https://claude.ai/install.sh | bash
claude --version

# VM resources: aim for 8-10 GB RAM and 6-8 vCPUs if the host allows
nproc && free -h
```

- Keep the repo on the VM's own disk, for example `~/code/realtime-fraud-detection`, not on a VMware shared folder.
- Claude Code runs in the VS Code terminal of the Remote-SSH session, so it executes on the VM. Documentation: https://code.claude.com/docs

### 15.3 Python packages (`pyproject.toml`)

```toml
[project]
name = "fraud"
version = "0.1.0"
readme = "README.md"
requires-python = ">=3.12,<3.13"
dependencies = [                     # runtime: everything the containers need
  "numpy", "pandas", "pyarrow", "duckdb",
  "redis", "networkx", "sortedcontainers",
  "scikit-learn", "xgboost", "joblib",
  "pydantic>=2", "pyyaml",
  "fastapi", "uvicorn[standard]", "httpx",
  "streamlit",
]

[dependency-groups]
dev = ["pytest", "pytest-cov", "ruff"]
sim = ["faker"]                                  # optional: readable merchant names only
analysis = ["shap", "matplotlib", "ipykernel"]   # offline plots and notebooks

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/fraud"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
  "redis: needs a Redis server at REDIS_URL",
  "slow: takes more than a few seconds",
]
```

| Group | Packages | Installed where |
|---|---|---|
| Core (runtime) | numpy, pandas, pyarrow, duckdb, redis, networkx, sortedcontainers, scikit-learn, xgboost, joblib, pydantic, pyyaml, fastapi, uvicorn, httpx, streamlit | VM venv and Docker image |
| dev | pytest, pytest-cov, ruff | VM venv and CI |
| sim (optional) | faker | VM venv |
| analysis (optional) | shap, matplotlib, ipykernel | VM venv |

**Pinning policy:**

- **Python version.** It lives in four places: `.python-version` (3.12), `requires-python`, the Dockerfile base image, and CI `python-version`, plus a mention in `CLAUDE.md`. Change all of them together or not at all.
- **Libraries.** Don't pin versions by hand in `pyproject.toml`. `uv.lock` pins exact versions; commit it and use `uv sync --frozen` everywhere.
- **Artifact-sensitive libraries** (xgboost, scikit-learn, numpy). Their versions are recorded in model metadata and checked at startup (§8). Run `uv lock --upgrade` only between phases, never during an experiment, and retrain after upgrading any of them.
- **Images and actions.** Pin the Redis and uv image tags exactly. Pin GitHub Action major versions.

---

## 16. Repository structure

```
realtime-fraud-detection/
├── CLAUDE.md                     # Claude Code project context (§18.2)
├── PLAN.md                       # this file
├── README.md                     # overview, architecture, results, benchmark, limitations, demo
├── Makefile
├── pyproject.toml  uv.lock  .python-version
├── .env.example                  # REDIS_URL, DATA_DIR, MODEL_DIR, MODEL_VERSION, CONSUMER_NAME, SPEEDUP
├── .gitignore  .dockerignore     # ignore data/, models/*, .venv, __pycache__, .tmp files
├── Dockerfile  docker-compose.yml
├── .github/workflows/ci.yml
├── configs/
│   ├── sim.yaml  sim_tiny.yaml   # population, patterns, hard negatives, seed
│   ├── cities.csv                # city, country, lat, lon, weight
│   ├── categories.yaml           # fixed category order + amount distributions + channel mix
│   ├── splits.yaml               # the ONLY place split dates live (§4.7)
│   ├── features.yaml             # windows, small-amount threshold, caps, defaults
│   ├── graph.yaml                # cadence, lookback, fan-out caps, label delay, Louvain seed, PPR alpha
│   ├── model.yaml                # rules, XGBoost search space, IF params, blend grid, budgets
│   ├── reason_codes.yaml         # feature -> analyst-readable template
│   └── stream.yaml               # stream/group names, batch size, flush policy, backpressure thresholds
├── src/fraud/
│   ├── config.py                 # typed config loading (YAML + env)
│   ├── schemas.py                # TransactionEvent and API models (shared)
│   ├── sim/                      # population.py  legit.py  patterns.py  generate.py  checks.py
│   ├── features/                 # spec.py  state.py  engine.py  store_memory.py  store_redis.py  replay.py  accounts.py
│   ├── graph/                    # projection.py  algorithms.py  snapshots.py  join.py  refresh_live.py
│   ├── modeling/                 # splits.py  metrics.py  rules.py  train_xgb.py  iforest.py  blend.py
│   │                             # decisions.py  experiments.py  evaluate_test.py  artifacts.py
│   ├── scoring/                  # risk_model.py (RiskModel, shared by scorer and API)  reasons.py
│   ├── stream/                   # replayer.py  scorer.py  sink.py  backfill.py  recovery.py
│   ├── storage/                  # duck.py (read-only views)  parquet_io.py (atomic writes)
│   └── api/                      # main.py  deps.py  routes/
├── dashboard/app.py
├── scripts/                      # entity_fanout_stats.py  benchmark_stream.py  rescore_check.py  export_results.py
├── tests/                        # §13, plus conftest.py
├── notebooks/                    # 01_sim_sanity.ipynb  02_error_analysis.ipynb (exploration only; no logic lives here)
├── reports/                      # committed: sim_report.md, feature_report.md, experiments/, results.md,
│                                 #            benchmark.md, test_runs.log, figures/
├── data/                         # gitignored (§3.7)
└── models/                       # gitignored except .gitkeep (§8)
```

**Responsibility rules:**

- `features/engine.py` is pure logic with no I/O. The stores do all I/O.
- `scoring/risk_model.py` is the only code that turns features into decisions. The scorer, the API and `rescore_check.py` all call it.
- `modeling/splits.py` is the only way to load split data.
- `storage/parquet_io.py` is the only code that writes Parquet.
- Notebooks may read outputs but never define logic that the pipeline depends on.

### 16.1 Makefile targets

| Target | Does |
|---|---|
| `setup` | `uv sync --all-groups` |
| `lint` / `format` | Ruff check + format check / auto-fix |
| `test` / `test-redis` / `test-all` | §13 |
| `redis-up` | `docker compose up -d redis` |
| `data` | `fraud.sim.generate` then `fraud.sim.checks` |
| `features` | `fraud.features.replay` |
| `graph` | `fraud.graph.snapshots` then `fraud.graph.join` |
| `experiments` | E1-E5 on **valid** |
| `train V=v1` | Build the final model (E4) and write `models/$(V)` |
| `evaluate-test V=v1` | `ALLOW_TEST=1`, final test metrics into `reports/` and metadata |
| `all` | `data features graph train` |
| `up` / `demo` / `logs` / `demo-reset` / `smoke` | §12.2 |
| `bench` / `rescore-check` | §9.6 |

All Python targets run through `uv run`. `REDIS_URL` defaults to `redis://localhost:6379/0` for host-side commands.

---

## 17. Phase-by-phase plan (14 days, ~8 focused hours per day)

### 17.1 Day map

| Day | Work | Checkpoint |
|---|---|---|
| 1 | Phase 0 (4 h) + Phase 1 (4 h) | CI green; Redis up |
| 2 | Phase 1 | |
| 3 | Phase 1 finish and freeze (4 h) + Phase 2 (4 h) | `sim-v1` tagged |
| 4 | Phase 2 | |
| 5 | Phase 2 finish (2 h) + Phase 3 (6 h) | **E2 on valid exists** |
| 6 | Phase 3 finish (2 h) + Phase 4 (6 h) | |
| 7 | Phase 4 finish | E3 + ablation on valid |
| 8 | Phase 5 | **`models/v1` + `results.md`** |
| 9 | Phase 6 | Redis store parity green |
| 10 | Phase 6 finish | **Live replay end to end + re-score check** |
| 11 | Phase 7 + Phase 8 | API + dashboard |
| 12 | Phase 9 | `make up` / `make demo` / `make smoke` |
| 13 | Phase 10 | README, results, CV bullets |
| 14 | Buffer: finish Phase 10, re-run everything from a clean clone, record the demo | Done |

Planned phase time totals ~107 h. The only real slack is Day 14, so the cut list (§2.4) is applied at the checkpoints, not at the end.

**Every phase ends the same way:**

1. `make lint && make test` (plus `make test-redis` from Phase 6 onwards) passes.
2. Commit, then tag `phase-N`.
3. Update `CLAUDE.md` if an invariant or command changed.

---

### Phase 0: Environment and skeleton (Day 1 morning, ~4 h, difficulty 2/10)

**Objective:** a repo where CI passes on an empty skeleton, Redis runs locally, and Claude Code has project context.

**Learn first:**

- uv basics: `sync`, `lock`, dependency groups, `run`.
- Docker Compose basics.
- Git branches and tags.

**Tasks:**

1. Verify the environment (§15.1 commands) and the VM allocation.
2. Create the GitHub repo and clone it to `~/code/realtime-fraud-detection`. Add `PLAN.md`.
3. Create the skeleton from §16: empty modules with docstrings, `pyproject.toml` (§15.3), `.python-version`, Makefile stubs, `.gitignore`, `.dockerignore`, `.env.example`.
4. Run `uv lock && uv sync --all-groups`.
5. Create `docker-compose.yml` with only the `redis` service. Run `make redis-up`.
6. Add `ci.yml` with the `lint-unit` job only, plus `tests/test_smoke.py`.
7. Create `CLAUDE.md` from §18.2. Commit and push.

**Files:** `pyproject.toml`, `uv.lock`, `.python-version`, `Makefile`, `docker-compose.yml`, `.github/workflows/ci.yml`, `CLAUDE.md`, `src/fraud/{__init__,config}.py`, `tests/test_smoke.py`

**Expected output:** a green CI badge; `docker compose exec redis redis-cli ping` prints `PONG`.

**Common mistakes:**

- Putting the repo on a VMware shared folder.
- Using Docker Desktop or the snap package instead of Docker Engine.
- Forgetting to log out and back in after joining the `docker` group.
- The Python version differing across `.python-version`, CI and `CLAUDE.md`.

**Completion checklist:**

- [ ] `make setup`, `make lint` and `make test` pass locally
- [ ] CI is green on GitHub
- [ ] Redis answers `PONG`
- [ ] `CLAUDE.md` is committed
- [ ] `PLAN.md` is committed

---

### Phase 1: Simulator (Day 1 afternoon to Day 3 midday, ~16 h, difficulty 5/10)

**Objective:** a deterministic generator of ~500K events with 4 fraud patterns and real hard negatives, validated and frozen.

**Learn first:**

- Poisson arrivals and exponential gaps.
- Lognormal distributions.
- `numpy.random.Generator` and `SeedSequence`.
- Haversine distance.
- The four patterns: be able to explain each in two sentences (§4.1).

**Tasks:**

1. **Configs:** `sim.yaml`, `sim_tiny.yaml`, `cities.csv` (15 Indian + 8 international cities, approximate coordinates), `categories.yaml`, `splits.yaml`.
2. **`population.py`:** accounts (including ones created during the simulation), merchants (including low-friction and colluding ones), devices (personal, family, upgrades), IP pools (home, carrier NAT, office/college, VPN).
3. **`legit.py`:** per-account generation (§4.4), including every hard negative.
4. **`patterns.py`:** four injectors, each returning events plus label rows (§4.5), with the ring constraints.
5. **`generate.py`:** assemble, stable-sort, assign `txn_id`, write the Parquet files and `manifest.json`.
6. **`checks.py`:** the §4.8 checks, written to `reports/sim_report.md` with a few example attacks.
7. **Tests:** `test_sim_*` (§13).
8. **Freeze:** record the `sim.yaml` hash in the report, commit, tag `sim-v1`.

**Expected output:** `data/raw/*.parquet` (~500K events, 1.2-1.8% fraud) and `reports/sim_report.md`.

**Common mistakes:**

- Looping over events in Python instead of vectorising per account.
- Label columns ending up in `events.parquet`.
- Card-testing victims being the attacker's own accounts; they must be random legitimate accounts.
- Legitimate travel implemented as instant jumps.
- Every young account being a ring member, because no legitimate accounts are created mid-simulation.
- Rings using NAT IPs, which the cap would then hide.

**Completion checklist:**

- [ ] `make data` runs in under ~3 minutes
- [ ] All `test_sim_*` tests pass
- [ ] The report shows every pattern and every hard negative in the expected ranges
- [ ] At least 12 rings start in the test window, and at least 4 of those reuse a device
- [ ] Same seed produces the same hashes
- [ ] `sim-v1` is tagged
- [ ] You can explain each pattern's mechanics and why each hard negative exists

---

### Phase 2: Feature engine and offline replay (Day 3 afternoon to Day 5 morning, ~14 h, difficulty 6/10)

**Objective:** the 30 hot features computed by one engine, plus the training replay and the backfill checkpoint.

**Learn first:**

- Event-time windows and half-open intervals.
- Running mean/variance from sums.
- Why features are computed before the state is updated.
- `sortedcontainers.SortedList`.

**Tasks:**

1. **`spec.py`:** `FEATURE_NAMES`, defaults, `FEATURE_SPEC_VERSION = "fs1"`, category levels.
2. **`state.py`:** `AccountState` with an exact JSON round trip.
3. **`accounts.py`:** `AccountDirectory`, loaded read-only from `accounts.parquet`.
4. **`engine.py`:**
   - `compute_features()` as a pure function.
   - `update_account()`.
   - `FeatureEngine.process()` with commit and idempotency hooks (§5.4).
5. **`store_memory.py`:** account blobs plus entity indexes with windowed counts.
6. **`replay.py`:** writes `hot_features.parquet` (split tag, burn-in flag, spec version) and the checkpoint at `test_start`.
7. **Tests:** `test_features_*`, `test_state_serialization.py`, `test_replay.py`. Use hand-computed sequences for every feature family.

**Expected output:** `data/features/hot_features.parquet`, `data/state/checkpoint_2026-03-14.json.gz`, `reports/feature_report.md`.

**Common mistakes:**

- Counting the current event inside its own window.
- Using `time.time()` anywhere in feature logic.
- Updating state before computing features.
- Z-scores blowing up when std is ~0 (use the floor in §5.2).
- Non-deterministic set order in the JSON.
- Mixing seconds and milliseconds.

**Completion checklist:**

- [ ] `make features` runs in under ~3 minutes
- [ ] Every feature has a hand-computed test
- [ ] Output row count equals the event count, with no NaNs
- [ ] The checkpoint loads back into an `InMemoryStore` and continues producing identical features (test)
- [ ] You can compute `acct_cnt_5m`, `amount_zscore` and `dev_accts_1h` by hand for a 5-event example

---

### Phase 3: Splits, baselines and XGBoost (Day 5 to Day 6 morning, ~8 h, difficulty 4/10)

**Objective:** a leak-free evaluation framework plus E1 (rules) and E2 (XGBoost on hot features), on **valid** only.

**Learn first:**

- PR curves vs ROC curves; average precision.
- Metrics at a threshold; alert budgets.
- XGBoost early stopping and categorical support.

**Tasks:**

1. **`splits.py`:** reads `splits.yaml`, excludes burn-in, and guards the test split behind `ALLOW_TEST`.
2. **`metrics.py`:** PR-AUC, capacity-constrained operating point, HOLD threshold search, per-pattern recall, value detection rate, false-positive rate, alert rate.
3. **`rules.py`:** R1-R4, evaluated on valid (E1).
4. **`train_xgb.py`:** 20-config random search on the hot features. Fit on train, early-stop on `early_stop`, select on valid (E2). Save parameter set P.
5. **`experiments.py`:** runner that writes `reports/experiments/E*.json`.
6. **Tests:** `test_splits.py`, `test_metrics.py`, `test_rules.py`, and the first part of `test_training_smoke.py`.

**Expected output:** E1 and E2 results on valid, and `xgb_search.json`.

**Common mistakes:**

- Peeking at test.
- Forgetting to drop burn-in rows.
- Feeding `attack_id` or `txn_id` into the model.
- Setting `scale_pos_weight` "because it's imbalanced".
- Judging models by accuracy.

**Completion checklist:**

- [ ] E1 and E2 JSON files exist
- [ ] E2 beats E1 on valid PR-AUC and recall at budget; if not, investigate before moving on
- [ ] Validation PR-AUC is not suspiciously perfect (> 0.995); if it is, apply the one-time realism rule (§4.8)
- [ ] You can explain why PR-AUC, and why an alert budget

---

### Phase 4: Graph layer (Day 6 afternoon to Day 7, ~14 h, difficulty 6/10)

**Objective:** 6 leak-free graph features, the training table, and the E3 ablation.

**Learn first:**

- Bipartite graphs and projections; why triangle-based clustering is 0 on bipartite graphs.
- The idea behind Louvain modularity.
- PageRank vs personalized PageRank.
- DuckDB `ASOF JOIN`.

**Tasks:**

1. Measure the accounts-per-device and accounts-per-IP distribution on the train period, then set `device_cap` and `ip_cap` in `graph.yaml` (§6.1).
2. **`projection.py`:** the SQL from §6.1, with `ORDER BY`.
3. **`algorithms.py`:** degree, clustering, seeded Louvain, community aggregates, personalized PageRank with delayed-label seeds, defaults.
4. **`snapshots.py`:** daily offline snapshots plus `calendar.parquet`. Log runtime per snapshot.
5. **`join.py`:** the two-step join (§6.4), then join labels to produce `training_table.parquet`.
6. **E3:** XGBoost with 36 features and parameter set P. Compare with E2: ring recall and overall recall on valid.
7. **Tests:** `test_graph_*`, `test_asof_join.py`.

**Expected output:** `data/graph/offline/`, `training_table.parquet`, E3 results, ablation numbers.

**Common mistakes:**

- Unsorted edges, which make Louvain non-deterministic.
- An `ASOF` join keyed on the account (the stale-value trap).
- Seeds that ignore the label delay.
- Including merchant edges.
- A cap high enough that card-testing devices fuse their victims.
- Using community IDs as features.

**Completion checklist:**

- [ ] All 89 snapshots are built in under ~10 minutes (otherwise go weekly and document it)
- [ ] Graph tests pass
- [ ] The ablation table exists
- [ ] E3 ring recall is noticeably above E2; if not, inspect ring cases before tuning anything
- [ ] You can draw the account-to-account graph for one ring and one family on paper and explain which features separate them

---

### Phase 5: Isolation Forest, blend, explanations and artifacts (Day 8, ~8 h, difficulty 5/10)

**Objective:** the final model (E4), leave-one-pattern-out evidence (E5), reason codes, `models/v1`, and one test evaluation.

**Learn first:**

- Isolation Forest intuition (anomalies are isolated in fewer splits).
- Mapping scores to percentiles.
- Leave-one-pattern-out validation.
- SHAP additivity.

**Tasks:**

1. **`iforest.py`:** fit on all train rows (label-free); store quantiles and feature stats.
2. **`blend.py`:** the four `XGB_-k` models, grid over `w`, choose `w*` (§7.6). Apply the fallback if `w* = 1.0`.
3. **`decisions.py`:** REVIEW and HOLD thresholds on valid (§7.7).
4. **`reasons.py`** + `reason_codes.yaml`: contributions → top 3 reasons, plus the anomaly reason.
5. **`artifacts.py`:** save/load `models/v1` with complete metadata; write `CURRENT`.
6. **`risk_model.py`:** `RiskModel.load()` / `score_batch()` with startup compatibility checks.
7. Run E4 and E5 on valid, then `make evaluate-test V=v1` **once**. Generate `reports/results.md` and the global contribution figure.
8. **Tests:** `test_scoring.py`, `test_reason_codes.py`, and the rest of `test_training_smoke.py`.

**Expected output:** `models/v1/`, `models/CURRENT`, `reports/results.md` (E1-E5 on test), `reports/test_runs.log`, a figure.

**Common mistakes:**

- Fitting Isolation Forest on valid or test data.
- Min-max scaling the anomaly score.
- Choosing `w` on known patterns only.
- Re-running the test evaluation while tweaking.
- Mismatched feature order in `pred_contribs`.
- Reason templates that refer to renamed features.

**Completion checklist:**

- [ ] `models/v1` loads through `RiskModel` with checks passing
- [ ] `results.md` has the E1-E5 tables
- [ ] Exactly one line in `test_runs.log` for v1
- [ ] Reason codes read sensibly for 10 sampled alerts (one per pattern at least)
- [ ] You can explain `w*` and what E5 shows, including if the result is negative

---

### Phase 6: Streaming pipeline (Days 9-10, ~16 h, difficulty 7/10)

**Objective:** the test window replayed through Redis, with identical features, idempotent recovery and a passing re-score check.

**Learn first:**

- Redis Streams: `XADD`, `XGROUP CREATE`, `XREADGROUP`, `XACK`, `XPENDING`, `XCLAIM`/`XAUTOCLAIM`, `XINFO GROUPS`.
- Pipelines vs `MULTI/EXEC` vs Lua.
- At-least-once vs exactly-once.
- Backpressure; watermarks.

**Tasks:**

1. **`store_redis.py`** (§5.4), plus `test_parity_redis.py` and `test_idempotency.py`. **Do this first.**
2. **`replayer.py`:** pacing modes, label stripping, backpressure.
3. **`sink.py`** + `storage/parquet_io.py` + `storage/duck.py`: atomic writes, watermark, deduplicating view.
4. **`scorer.py`:**
   - main loop with micro-batching;
   - pending-message drain and reclaim;
   - DLQ;
   - ack after flush;
   - HOLD annotation;
   - metrics;
   - SIGTERM handling;
   - `crash_after` test hook.
5. **`backfill.py`** (§9.5).
6. **`graph/refresh_live.py`** (§6.5).
7. **`scripts/rescore_check.py`** (§9.6, item 3).
8. **End-to-end run:** Redis in Compose; replayer, scorer and graph-refresh from the VM venv in three terminals; replay the full test window at 3,600x.
9. **Tests:** `test_scorer_recovery.py`, `test_sink.py`, `test_replayer.py`, `test_graph_live.py`.

**Expected output:** `data/scored/`, `data/graph/live/`, and a passing re-score check.

**Common mistakes:**

- Acknowledging before the flush.
- Creating the group at `$` after the replay has started.
- Letting the scorer or refresh job read labels, or read test-window rows from the simulator file.
- Using wall-clock time in windows.
- Forgetting to flush on shutdown.
- An unbounded stream.
- Not ignoring `BUSYGROUP`.
- Bytes vs str confusion (pick `decode_responses=True` everywhere).

**Completion checklist:**

- [ ] Redis parity and idempotency tests pass
- [ ] Every crash point in §9.3 is tested
- [ ] The full test window is scored with exactly one unique row per event
- [ ] Re-score check: 100% identical
- [ ] Live graph snapshot equals offline for 3 boundaries
- [ ] You can walk through the failure matrix without notes

---

### Phase 7: FastAPI (Day 11 morning, ~5 h, difficulty 3/10)

**Objective:** the §10 endpoints, backed by the same `RiskModel` and `FeatureEngine`.

**Learn first:**

- FastAPI lifespan and dependencies.
- Pydantic v2 validation (`extra="forbid"`, `Literal`).
- `TestClient`.

**Tasks:**

1. `schemas.py` models.
2. `main.py` with lifespan.
3. The five routes.
4. The 503 handler.
5. `test_api.py`, including the check that `/score` does not change `state:acct:*`.

**Expected output:** `uvicorn fraud.api.main:app` serves the endpoints, and `/docs` shows the schemas.

**Common mistakes:**

- Loading the model per request.
- Async endpoints with the sync Redis client.
- `/score` writing state.
- Opening a DuckDB *file* instead of an in-memory connection over Parquet.

**Completion checklist:**

- [ ] All endpoints return the documented shapes
- [ ] 422, 404 and 503 cases are tested
- [ ] `/health` reports the model version and the latest snapshot

---

### Phase 8: Dashboard (Day 11 afternoon, ~4 h, difficulty 3/10)

**Objective:** the six panels from §11, refreshing live during a replay.

**Learn first:** `st.fragment(run_every=...)`, `st.cache_resource`, `st.dataframe`.

**Tasks:**

1. `dashboard/app.py` with the panels.
2. Label the replay scorecard as evaluation data.
3. Manual check during a replay.

**Common mistakes:**

- Caching query results for too long.
- Heavy queries on every refresh.
- Presenting the scorecard as if the scorer saw labels.

**Completion checklist:**

- [ ] The dashboard updates during a replay
- [ ] Selecting an alert shows its reasons
- [ ] The page works at 1280 px wide

---

### Phase 9: Docker Compose and benchmark (Day 12, ~8 h, difficulty 5/10)

**Objective:** one-command stack, smoke test, and E6 numbers.

**Learn first:**

- Dockerfile layer caching.
- Compose `depends_on` conditions, healthchecks and profiles.
- Bind-mount permissions.

**Tasks:**

1. Dockerfile (§12.1). Replace `<uv-version>` and the Redis tag with exact versions.
2. Full `docker-compose.yml` (§12.2); `.env.example` values for Compose.
3. `make up`, `make demo`, `make demo-reset`, `make smoke`.
4. `scripts/benchmark_stream.py`: capacity at batch sizes {1, 50, 200, 500}, then latency at 50% and 80% of capacity.
5. Run `make rescore-check` inside the Compose run as well.
6. Write `reports/benchmark.md` with hardware details and `docker stats` peaks.
7. Add the `redis-integration` and `image-build` jobs to CI.

**Common mistakes:** listed in §12.2.

**Completion checklist:**

- [ ] `make demo-reset && make up && make demo` works from scratch
- [ ] `make smoke` passes
- [ ] `benchmark.md` has capacity, p50/p95/p99, staleness and memory
- [ ] CI has three green jobs

---

### Phase 10: Documentation, demo and interview preparation (Day 13 and Day 14 morning, ~10 h, difficulty 3/10)

**Objective:** a repository that a reviewer understands in two minutes and that you can defend for an hour.

**Tasks:**

1. **README**, in this order:
   - one-paragraph summary including the synthetic-data caveat;
   - architecture diagram;
   - quickstart (`make setup all up demo`);
   - results table and ablation from `results.md`;
   - benchmark with hardware;
   - design decisions (link to §3.8);
   - failure handling (the §9.3 table);
   - limitations;
   - "what I'd change at scale" (§9.7);
   - the environment you actually used.
2. **Demo recording (2-3 minutes).** Stack up, replay starting, dashboard updating, one alert opened with its reasons, `/health` and `/docs`, `docker compose restart scorer` mid-replay showing recovery with no duplicates. Link it from the README.
3. **CV bullets.** Fill the placeholders in §20.3 from `results.md` and `benchmark.md`.
4. **Interview prep.** Rehearse the §21 questions out loud; add answers to `docs/interview_notes.md` (optional, not linked from the README).
5. **Clean-clone rerun (Day 14).** Clone into a new directory, run `make setup all up demo`, and confirm the numbers match `reports/`.

**Completion checklist:**

- [ ] The clean clone reproduces the pipeline
- [ ] Every README number traces to a file in `reports/`
- [ ] The demo recording is linked
- [ ] CV bullets are filled with measured values
- [ ] You can answer every §21 question without notes

---

## 18. Claude Code workflow

### 18.1 Working loop

1. **Start a session.** Run `claude` from the repo root in the VS Code terminal of your Remote-SSH session, so it runs on the VM. `CLAUDE.md` is loaded at session start.
2. **One task per prompt.** Each prompt names the PLAN.md sections and the files involved (§18.3). Never ask for a whole phase in one go.
3. **Plan before editing.** For anything that touches more than one file, switch to plan mode first: press Shift+Tab until the mode indicator shows plan mode, or prefix the prompt with `/plan`. Read the plan. If it touches files you didn't expect, ask why before approving.
4. **Tests first for tricky logic** (windows, joins, recovery). Ask for failing tests built from hand-computed examples, then ask for the implementation. The implementation must not change the expected values.
5. **Verify yourself.**
   - Run `make lint && make test` yourself, or have Claude Code run them and show the output.
   - Read the `git diff`.
   - For any function you can't explain, ask "walk me through this line by line", then rewrite the explanation in your own words.
6. **Commit per task** with a descriptive message. Tag the end of each phase (`phase-N`).
7. **Keep context small.**
   - Use `/clear` between unrelated tasks.
   - Start a fresh session per phase.
   - `PLAN.md` and `CLAUDE.md` carry the durable context. If Claude Code keeps its own notes between sessions, these two files still win.
8. **Never accept a claimed metric.** Run the command and read `reports/`.
9. **Stop and decide yourself** whenever Claude Code proposes:
   - a new dependency;
   - a new service;
   - a change to `configs/sim.yaml` or `configs/splits.yaml`;
   - touching the test split.
10. **Permission modes.**
    - Use the default mode (asks before edits and commands) through Phase 2, while patterns settle.
    - Accept-edits mode is fine later for tests and boilerplate.
    - Don't use bypass-permissions mode on a VM that holds your SSH keys.
    - See https://code.claude.com/docs/en/permission-modes

### 18.2 `CLAUDE.md` template (create in Phase 0)

```markdown
# CLAUDE.md: realtime-fraud-detection

## What this is
Near-real-time transaction fraud monitoring on synthetic data:
simulator -> FeatureEngine (hot features) + graph snapshots (warm features)
-> XGBoost + Isolation Forest blend -> Redis Streams scorer -> Parquet/DuckDB, FastAPI, Streamlit.
The full plan is PLAN.md; prompts reference its section numbers.

## Environment
- Ubuntu 24.04 VM over VS Code Remote-SSH. Python 3.12 via uv (`uv run ...`). Docker Engine + Compose v2.
- Redis: `make redis-up` (REDIS_URL=redis://localhost:6379/0; tests use DB 15).
- CPU only (i5-1235U, 16 GB host). Keep runtimes small; no GPU code.

## Commands
- make setup | lint | format | test | test-redis | test-all
- make data | features | graph | experiments | train V=v1 | evaluate-test V=v1 | all
- make up | demo | logs | demo-reset | smoke | bench | rescore-check

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

## Out of scope (do not add)
MLflow, Optuna, CatBoost, LightGBM, PostgreSQL, Kafka, Spark, Airflow, Neo4j, GNNs, Kubernetes,
cloud deployment, auth, Prometheus/Grafana, PyOD, extra services.
Ask before adding ANY dependency.

## Conventions
- Type hints everywhere. Pure functions in features/ and graph/algorithms.py. I/O only in stores, storage/ and entrypoints.
- All config via src/fraud/config.py (YAML + env). No hard-coded paths, dates or thresholds.
- Deterministic: seeded RNGs, ORDER BY before building graphs, sorted JSON.
- Use the logging module; no print() in library code. Ruff for lint and format (line length 100).

## Working agreement
- Before editing more than one file, show a short plan and the file list.
- Write or update tests in the same change. Run `make lint && make test`
  (plus `make test-redis` when Redis code changed) and show the output.
- Do not modify configs/sim.yaml, configs/splits.yaml or reports/ unless the task says so.
- Never state a metric or benchmark result; point to the command that produces it.
- If PLAN.md and the code disagree, stop and ask.
```

### 18.3 Prompts by phase

Copy each prompt as-is. Every prompt is scoped to a few files.

For each phase, **You verify** is what you check yourself before accepting the work. **You must explain** is what you should be able to say without notes before moving on.

#### Phase 0

> **P0.1** Read PLAN.md §15 and §16 and CLAUDE.md. Create the repository skeleton from §16:
>
> - empty modules with one-line docstrings
> - `pyproject.toml` exactly as in §15.3, and `.python-version` (3.12)
> - `.gitignore`, `.dockerignore` and `.env.example` (§16)
> - a Makefile with the §16.1 targets (stubs that print "not implemented" are fine for later phases)
> - `tests/test_smoke.py`
> - `src/fraud/config.py`: loads YAML from `configs/`, with env overrides for REDIS_URL, DATA_DIR, MODEL_DIR and MODEL_VERSION
>
> No business logic. Show the plan first.

> **P0.2** Add `docker-compose.yml` with only the redis service from §12.2, and `.github/workflows/ci.yml` with only the lint-unit job from §14. Then run `uv lock`, `uv sync --all-groups`, `make lint`, `make test`, `make redis-up` and `docker compose exec redis redis-cli ping`. Show all outputs.

- **You verify:** CI is green after pushing; the tree matches §16.
- **You must explain:** what `uv.lock` is for, and why CI uses `--frozen`.

#### Phase 1

> **P1.1** Implement PLAN.md §4.2-§4.3:
>
> - `configs/sim.yaml` and `configs/sim_tiny.yaml`
> - `configs/cities.csv` (15 Indian + 8 international cities, approximate lat/lon, weights)
> - `configs/categories.yaml` (10 categories in a fixed order, with lognormal median/sigma and POS/ONLINE mix)
> - `configs/splits.yaml` (§4.7)
> - `src/fraud/sim/population.py`:
>   - accounts, including ones created mid-simulation
>   - merchants, including 20 low-friction and 60 colluding
>   - devices, including families and phone upgrades
>   - IP pools: home, carrier NAT, office/college, VPN
>
> Use numpy Generators spawned from a SeedSequence. No event generation yet. Add `tests/test_sim_population.py`. Plan first.

> **P1.2** Implement `src/fraud/sim/legit.py` per §4.4, including every hard negative in that table.
>
> - Vectorise within each account; no Python loop over all events.
> - Output the §4.2 event columns and no label columns.
> - Add `tests/test_sim_hard_negatives.py` using `sim_tiny.yaml`.
> - Report the runtime on the full config.

> **P1.3** Implement `src/fraud/sim/patterns.py` per §4.5.
>
> - Four injectors, each returning (events, labels) with `fraud_type`, `attack_id`, `ring_id` and `label_available_at = event_time + 14 days`.
> - Velocity, ATO and card-testing victims are existing legitimate accounts.
> - Enforce the ring constraints:
>   - device-reuse share;
>   - ≥ 12 rings starting in the test window, ≥ 4 of them reusing devices;
>   - ≥ 20 rings entirely inside train.
>
> Add `tests/test_sim_patterns.py`. Plan first.

> **P1.4** Implement `src/fraud/sim/generate.py` and `checks.py` per §4.2 and §4.8.
>
> - Assemble, stable-sort and assign `txn_id`.
> - Write `data/raw/*.parquet` (zstd) and `manifest.json`.
> - Write `reports/sim_report.md` with every §4.8 check plus one example attack per pattern.
> - Add `tests/test_sim_determinism.py` and `tests/test_sim_schema.py`.
> - Wire `make data`, run it, and show the report summary.

- **You verify:** read `sim_report.md`, then list one card-testing attack yourself:

  ```sql
  SELECT e.* FROM 'data/raw/events.parquet' e
  JOIN 'data/raw/labels.parquet' l USING (txn_id)
  WHERE l.attack_id = (SELECT min(attack_id) FROM 'data/raw/labels.parquet' WHERE fraud_type = 'CARD_TESTING')
  ORDER BY e.event_time;
  ```

  Confirm there are no label columns in `events.parquet`, then tag `sim-v1`.
- **You must explain:** every pattern, every hard negative, and why `SeedSequence.spawn` gives independent streams.

#### Phase 2

> **P2.1** Read PLAN.md §5.1-§5.3. Before implementing anything, write `tests/test_features_windows.py` and `tests/test_features_geo.py` from hand-computed examples:
>
> - Half-open windows: events at 10:00:00, 10:04:59 and 10:05:00 give `acct_cnt_5m = 2` for the last one.
> - Cold-start defaults.
> - `amount_zscore` with the std floor.
> - `hour_unusualness` smoothing.
> - `new_device` and `new_city`.
> - `geo_speed_kmh` with the 1-minute floor and the 5,000 cap.
> - Haversine Delhi-Mumbai ≈ 1,150 km.
> - Fan-out counts using last-seen per account.
>
> Tests may import functions that don't exist yet. Do not implement them.

> **P2.2** Implement `src/fraud/features/spec.py`, `state.py`, `accounts.py` and `engine.py` per §5.2-§5.4:
>
> - `compute_features` as a pure function
> - `update_account`
> - `FeatureEngine.process` with the commit and idempotency hooks
>
> Also implement `store_memory.py`. Make the P2.1 tests pass **without changing their expected values**. If you think an expected value is wrong, stop and explain why.

> **P2.3** Implement `src/fraud/features/replay.py` per §5.5:
>
> - split tag from `splits.yaml`
> - burn-in flag
> - checkpoint at `test_start`
>
> Add `tests/test_replay.py` and `tests/test_state_serialization.py`. Wire `make features`, run it on the full data, report the runtime, and write `reports/feature_report.md`.

- **You verify:** pick 3 rows from `hot_features.parquet` and recompute them by hand from the events with DuckDB.
- **You must explain:** compute-then-update; variance from running sums; how last-seen gives distinct-account counts.

#### Phase 3

> **P3.1** Implement `src/fraud/modeling/splits.py`, `metrics.py` and `rules.py` per §4.7, §7.3, §7.7 and §7.8, with `test_splits.py`, `test_metrics.py` and `test_rules.py`.
>
> - `splits.load("test")` must raise unless `ALLOW_TEST=1`.
> - Operating point: the threshold with the highest F1 whose alert rate is ≤ `review_budget`.
> - HOLD threshold: the lowest threshold with precision ≥ 0.95 and at least 50 alerts.

> **P3.2** Implement `train_xgb.py` and `experiments.py` per §7.4 and §7.9, for **E1 and E2 only** (hot features, valid split only).
>
> - 20-config random search, early stopping on `early_stop`, selection by valid PR-AUC.
> - Save `reports/experiments/xgb_search.json`, `E1.json` and `E2.json`.
> - Report total training time.
> - Never touch the test split.

- **You verify:** E2 beats E1. Look at the 20 highest-risk false positives on valid: are they the hard negatives you designed?
- **You must explain:** PR-AUC vs accuracy, the alert budget, and early stopping.

#### Phase 4

> **P4.1** Write `scripts/entity_fanout_stats.py`. On the train period, it reports the distribution of distinct accounts per device and per IP over 30-day windows (percentiles 50/90/99/99.5/99.9). **Then stop.** I will set `device_cap` and `ip_cap` in `configs/graph.yaml` myself (§6.1).

> **P4.2** Implement `src/fraud/graph/projection.py` and `algorithms.py` per §6.1-§6.3, with `test_graph_projection.py`, `test_graph_algorithms.py` and `test_graph_leakage.py`.
>
> - Toy data: one family, one ring, one over-cap IP.
> - Clustering is 0 on the raw bipartite toy graph and > 0 on the account-to-account graph.
> - The label delay is respected.
> - Keep it deterministic: `ORDER BY` on edges, seeded Louvain, sorted seeds.

> **P4.3** Implement `src/fraud/graph/snapshots.py` (daily offline snapshots, calendar, runtime log) and `join.py` (the two-step join in §6.4, then labels, producing `training_table.parquet`).
>
> - Add `test_asof_join.py` covering the three edge cases in §6.4.
> - Wire `make graph`, run it, and report the total runtime.

> **P4.4** Add E3 to `experiments.py` (36 features, the same parameter set P as E2). Add an ablation table: E2 vs E3, overall and per-pattern recall at the operating point, valid split. Show the table.

- **You verify:** pick one ring from `labels.parquet`, pull its accounts' graph features at one snapshot, and compare them with a family's.
- **You must explain:** why the graph is projected, why the caps exist, why personalized PageRank uses a label delay, and the ASOF trap.

#### Phase 5

> **P5.1** Implement `iforest.py` and `blend.py` per §7.5-§7.6:
>
> - label-free Isolation Forest on all train rows (35 numeric features)
> - 1,001-quantile percentile mapping
> - feature medians and IQRs
> - the four leave-one-pattern-out XGBoost models
> - the `w` grid and the selection of `w*`
>
> Save the E5 inputs under `reports/experiments/`. **If `w* == 1.0`, stop and tell me before implementing the fallback.**

> **P5.2** Implement `decisions.py`, `scoring/reasons.py` (plus `configs/reason_codes.yaml` covering all 36 features), `modeling/artifacts.py` and `scoring/risk_model.py` per §7.7, §7.10 and §8.
>
> - Add `test_scoring.py` and `test_reason_codes.py` (contributions sum to the margin within 1e-4).
> - `make train V=v1` must write `models/v1` with complete metadata, plus `models/CURRENT`.

> **P5.3** Implement `modeling/evaluate_test.py` per §7.11, and `scripts/export_results.py`, which renders `reports/results.md` (the §7.9 tables) and the global contribution figure. **Do not run the test evaluation**; I will run `make evaluate-test V=v1` myself.

- **You verify:** run the test evaluation once yourself, read `results.md`, and read the reasons for 10 sampled alerts.
- **You must explain:** how `w*` was chosen, what E5 shows (even if negative), and why HOLD precision matters.

#### Phase 6

> **P6.1** Implement `src/fraud/features/store_redis.py` per §5.3-§5.4:
>
> - one pipeline for load and one `MULTI/EXEC` for commit
> - `ZADD GT` and `ZCOUNT` with an exclusive upper bound
> - periodic trimming
> - `decode_responses=True`
>
> Add the redis-marked tests `test_parity_redis.py` (in-memory vs Redis on `tiny_sim`: identical features) and `test_idempotency.py`. Run `make test-redis` and show the output.

> **P6.2** Implement:
>
> - `storage/parquet_io.py` (atomic temp-file + rename writes)
> - `storage/duck.py` (in-memory DuckDB with the deduplicating view from §9.4)
> - `stream/sink.py` (buffer, flush policy, watermark)
> - `stream/replayer.py` (§9.1: pacing modes, label stripping, backpressure)
>
> Add `test_sink.py` and `test_replayer.py`.

> **P6.3** Implement `stream/scorer.py` and `stream/recovery.py` per §9.2-§9.3. The scorer must:
>
> - drain its own pending messages at startup
> - reclaim periodically with XPENDING/XCLAIM, applying the delivery-count DLQ rule
> - run the micro-batch loop
> - resolve the graph snapshot before commit
> - score with `RiskModel`, with reasons for REVIEW/HOLD
> - apply the HOLD annotation
> - acknowledge only after the flush
> - publish metrics
> - flush on SIGTERM
> - expose a `crash_after` hook
>
> Add `test_scorer_recovery.py` covering every row of the §9.3 table. Plan first; this is the most important module.

> **P6.4** Implement:
>
> - `stream/backfill.py` (§9.5)
> - `graph/refresh_live.py` (§6.5, including the guard that history is read only for `event_time < test_start`), with `test_graph_live.py`
> - `scripts/rescore_check.py` (§9.6, item 3)
>
> Then give me the exact commands to run the end-to-end replay in three terminals.

- **You verify:** run the replay yourself. Stop the scorer with Ctrl+C midway and restart it. Confirm one unique row per event with DuckDB, and run the re-score check.
- **You must explain:** the failure matrix, the watermark rule, and why the ack comes after the flush.

#### Phase 7

> **P7.1** Implement the FastAPI service per §10:
>
> - schemas in `src/fraud/schemas.py` (`extra="forbid"` and the listed constraints)
> - `api/main.py`, whose lifespan loads `RiskModel`, Redis and `FeatureEngine` once
> - the five routes
> - a 503 handler for Redis connection errors
> - `test_api.py`, including:
>   - a body containing `is_fraud` returns 422
>   - `/score` leaves `state:acct:*` unchanged
>   - an unknown transaction returns 404
>
> Use sync `def` endpoints only.

- **You verify:** open `/docs`; call `/score` with curl; confirm the Redis keys didn't change.
- **You must explain:** what lifespan does, and why `/score` is read-only.

#### Phase 8

> **P8.1** Build `dashboard/app.py` per §11:
>
> - `st.fragment(run_every=...)` per panel
> - a cached in-memory DuckDB connection
> - API calls through httpx (`API_URL` from env)
> - the replay scorecard labelled as evaluation data the scorer never sees
>
> Keep the default styling.

#### Phase 9

> **P9.1** Write the Dockerfile and the full `docker-compose.yml` from §12, using these exact tags: uv `<fill in>`, Redis `<fill in>`.
>
> - Update `.env.example` for Compose.
> - Implement `make up`, `make demo`, `make logs`, `make demo-reset` and `make smoke`.
> - Comment every Dockerfile line.
>
> Then run `make demo-reset`, `make up` and `make smoke`, and show the output.

> **P9.2** Implement `scripts/benchmark_stream.py` per §9.6:
>
> - capacity at batch sizes 1/50/200/500
> - latency at 50% and 80% of capacity using `--rate`
> - staleness
> - peak memory, sampled from `docker stats --no-stream`
>
> It writes `reports/benchmark.md` with a hardware section that I will fill in. Also add the `redis-integration` and `image-build` CI jobs from §14.

#### Phase 10

> **P10.1** Draft `README.md` per Phase 10 of §17, using only numbers from `reports/results.md` and `reports/benchmark.md`, and name the source file for every number.
>
> - Put the synthetic-data caveat in the first paragraph.
> - Include the §9.3 failure table, the limitations, and the §9.7 "at scale" section.
> - Mark any section you can't fill from files as TODO instead of inventing content.

> **P10.2** Fill the CV bullet template in §20.3 from `reports/`. For each number, list the file and line it came from. Say so if any placeholder can't be filled.

> **P10.3** Quiz me on the §21 questions, one at a time. Wait for my answer, then point to the file or section that supports or contradicts it.

---

## 19. Difficulty

| Part | Difficulty (1-10) | Why |
|---|---|---|
| Simulator | 5 | Realistic legitimate behaviour and hard negatives take care, but no new theory |
| Feature engine | 6 | Event-time windows, incremental statistics, parity by construction |
| Graph layer | 6 | Projection, caps, label delay and point-in-time joins; each is simple, the combination needs care |
| Modelling | 4-5 | Familiar tools; the new ideas are leave-one-pattern-out and the decision policy |
| Streaming | 7 | Delivery semantics, idempotency, recovery and watermarks (the hardest phase) |
| API, dashboard | 3 | Thin layers |
| Docker, CI | 4-5 | Familiar from Delivery Delay, plus healthchecks, profiles and a Redis service container |
| **Overall** | **6 / 10** | Inside the 5-6 target |

**Why it isn't lower.** Parity and idempotency are real engineering requirements, and each is backed by a test.

**Why it isn't higher.** Everything runs on one node with one scorer, a batch graph, one model and no cloud.

**Over-complex version to avoid (about 9/10):**

- Kafka + Flink stateful processing
- Feast
- Neo4j with streaming community detection
- GNN embeddings
- MLflow registry
- Kubernetes and cloud deployment
- Online learning
- An LLM analyst copilot
- A synchronous authorization path with a sub-10 ms budget

Each item is defensible at a company. Together they are months of work for a student, with shallow understanding of every piece.

**Fallback if Days 9-10 slip (about 5/10).** Apply the cut list (§2.4):

- Weekly snapshots.
- No DLQ.
- No `/transactions/{id}` endpoint.
- **Keep the streaming layer and its idempotency.** That is this project's domain in the portfolio.

---

## 20. Final recommended project

### 20.1 Summary

| Item | Final answer |
|---|---|
| **Title** | Real-Time Transaction Fraud Detection System |
| **One-liner** | Streaming fraud monitoring that combines behavioural, graph and anomaly signals, with idempotent processing and explained alerts |
| **Architecture** | §3.2. Offline: simulator → one feature engine → graph snapshots → point-in-time join → XGBoost + Isolation Forest. Online: Redis Streams → idempotent scorer → Parquet/DuckDB, with live graph refresh, FastAPI and Streamlit. |
| **Feature set** | 36 = 30 per-event features (context, velocity, deviation, novelty/location, entity fan-out) + 6 graph features (§5.2) |
| **Repository** | §16 |
| **Phases** | §17 (14 days) |
| **Installs** | §15 |
| **Difficulty** | 6/10 (§19) |
| **What not to build** | §2.3 |

| Layer | Tools |
|---|---|
| Language and tooling | Python 3.12, uv, Ruff, pytest, Make |
| Data generation | NumPy, pandas, PyArrow (Faker optional) |
| Streaming and state | Redis Streams (consumer groups); Redis JSON blobs and sorted sets |
| Graph | DuckDB SQL (projection), NetworkX (Louvain, personalized PageRank) |
| Storage and analytics | Parquet, DuckDB (ASOF joins, monitoring SQL) |
| Models | XGBoost; scikit-learn Isolation Forest |
| Explainability | XGBoost native SHAP contributions (`shap` offline only) |
| Serving and UI | FastAPI, Pydantic v2, uvicorn, Streamlit |
| Packaging and CI | Docker, Docker Compose, GitHub Actions |

### 20.2 Interview value

With evidence in the repo, you can discuss:

- **Streaming system design:** at-least-once delivery, idempotent state, ordering, backpressure, replay, dead-letter queues, watermarks, failure handling.
- **Feature correctness:** offline/online parity, point-in-time correctness, a concrete list of leakage rules and their tests.
- **Graph fraud analytics:** projection, fan-out caps, communities, risk propagation, delayed labels.
- **Imbalanced classification:** PR-AUC, alert budgets, two-tier decisions.
- **Hybrid modelling:** supervised plus unsupervised, justified with a leave-one-pattern-out protocol, including an honest negative result if that's what you find.
- **Analyst-facing explanations:** reason codes, with a test that they match the model.
- **Storage choices:** DuckDB's concurrency model, Parquet layout, single-writer design.
- **Measurement:** throughput, latency, staleness, and limitations stated upfront.

This covers data engineering, ML engineering, data science and backend-AI interviews, with fintech as the domain. It also doesn't repeat the Delivery Delay story (§0.1).

### 20.3 CV bullets (template; fill in after Phase 10)

**Real-Time Transaction Fraud Detection System** | Python, Redis Streams, NetworkX, XGBoost, FastAPI, DuckDB, Docker

Objective: Built a **streaming** fraud monitoring pipeline that scores card transactions using **behavioral**, **graph-based** and **anomaly** signals

- Simulated **500K+** transactions with **4** injected fraud patterns (velocity abuse, account takeover, card testing, fraud rings) alongside legitimate look-alikes
- Engineered **[36]** leakage-safe features spanning **transaction velocity**, **spending deviation**, **device/IP fan-out**, **personalized PageRank** and **Louvain** community statistics
- Trained an **XGBoost + Isolation Forest** ensemble reaching **[R]% recall**, **[F] F1** and **[P] PR-AUC** on a time-split test set; graph features lifted fraud-ring recall from **[a]%** to **[b]%**
- Developed a **Redis Streams** consumer-group pipeline with idempotent processing, **FastAPI**, **DuckDB** analytics and **SHAP** reason codes, handling **[N] events/s** at **[L] ms** p95 latency

**Rules for filling the placeholders:**

- **Recall, F1 and PR-AUC:** from `reports/results.md`, using E4 on test at the REVIEW operating point.
- **Ring lift:** E2 vs E3 on test.
- **N:** the measured capacity.
- **L:** p95 at 80% of capacity, from `reports/benchmark.md`, which names the run.

**If the results differ from the template:**

- If recall is below 92%, write the real number.
- If the ring lift is small, drop that clause.
- If E5 shows no benefit, write "XGBoost classifier" and move Isolation Forest to a "negative results" section of the README.

**Wording:**

- "Real-Time" in the title is fine if p95 is well under a second and the README says "post-authorization monitoring" (§0.3).
- If a bullet runs past two lines in your CV template, cut the parenthetical pattern list or the ring clause first.
- The leading verbs (Built, Simulated, Engineered, Trained, Developed) are all different; keep it that way if you edit.

**What changed from your original bullets, and why:**

| Original | Revised | Why |
|---|---|---|
| "real-time ... using behavioral, graph-based, anomaly features" | "streaming ... anomaly signals" | Scoring happens after the payment; the Isolation Forest output is a score, not a feature |
| "4 injected fraud patterns including ..." | "4 injected fraud patterns (...) alongside legitimate look-alikes" | There are exactly four; the look-alikes are what make the metrics credible |
| "30+ ... PageRank, community detection" | "[36] leakage-safe ... personalized PageRank and Louvain community statistics" | Plain PageRank mostly repeats degree; community IDs aren't usable features |
| "~92% recall, 0.90 F1, 0.93 PR-AUC on injected fraud patterns" | "[R] / [F] / [P] on a time-split test set" + ring lift | The numbers are measured, not targeted; the ablation is the strongest evidence you'll have |
| "Redis Streams + FastAPI scoring pipeline with DuckDB persistence and SHAP-based explanations for flagged transaction" | Adds idempotent consumer groups and measured throughput/latency | These are what data-engineering interviewers probe |

**Evidence map (every claim has a file behind it):**

| Claim | Evidence |
|---|---|
| 500K+ transactions, 4 patterns, look-alikes | `reports/sim_report.md`, `make data` |
| 36 leakage-safe features | `src/fraud/features/spec.py`; the tests listed in §7.1 |
| Personalized PageRank, Louvain | `src/fraud/graph/algorithms.py`; ablation in `reports/results.md` |
| Recall, F1, PR-AUC | `reports/results.md` (E4, test); `reports/test_runs.log` |
| Ring lift | `reports/results.md` (E2 vs E3) |
| Ensemble | `reports/results.md` (E5) |
| Idempotent consumer groups | `tests/test_scorer_recovery.py`, `tests/test_idempotency.py`, the demo recording |
| Events/s, p95 latency | `reports/benchmark.md` |
| Reason codes | `configs/reason_codes.yaml`, the dashboard, `GET /transactions/{id}` |

---

## 21. Interview preparation

| # | Question | Short answer (details in) |
|---|---|---|
| 1 | Why not accuracy? | At ~1.5% fraud, "always legitimate" scores 98.5%. I report PR-AUC plus recall and precision at a fixed analyst budget. (§7.8) |
| 2 | What does HOLD do if the payment already went through? | It freezes the card or account for what follows, which is how card testing and takeover are contained. (§0.3, §7.7) |
| 3 | How do you prevent leakage in windowed features? | Event time, half-open windows, compute before update, time-based splits, and a test per rule. (§5.1, §7.1) |
| 4 | Why was your first graph's clustering coefficient zero? | Account-to-entity graphs are bipartite, so there are no triangles. I project to an account-to-account graph. (§6.1) |
| 5 | Why cap shared devices and IPs? | Carrier NAT, offices and card-testing devices link unrelated people. Without caps, victims fuse into fake rings and risk spreads to them. (§6.1) |
| 6 | Why personalized PageRank with a label delay? | It measures closeness to known fraud. The delay mirrors chargeback timing, so the feature isn't optimistic. (§6.2-§6.3) |
| 7 | Why not ASOF-join graph features on `account_id`? | That returns stale values for accounts that dropped out of later snapshots. I resolve the snapshot first, then left-join with defaults. (§6.4) |
| 8 | How do offline and online features match? | One engine, two stores, a parity test, and an offline re-score of live output. (§5.4, §9.6) |
| 9 | The scorer crashes after committing state but before acking. What happens? | The message is redelivered, the stored feature record is returned, and state is unchanged. Duplicate rows are deduplicated on `txn_id`. (§9.3) |
| 10 | Why acknowledge after the flush? | Acknowledging earlier can lose events if the process dies before the write. (§9.2) |
| 11 | How is ordering preserved? How would you scale out? | One ordered scorer. To scale, partition by account (Kafka-style) and have the graph job wait for the minimum watermark. (§9.2, §9.7) |
| 12 | Why Redis Streams and not Kafka? | Same concepts with one container, and it doubles as the state store. Kafka is the answer at higher volume. (§3.8, §9.7) |
| 13 | Why Parquet + DuckDB? | DuckDB allows one read-write process or many readers. Single-writer Parquet with in-memory DuckDB readers avoids lock conflicts. (§3.8, §9.4) |
| 14 | Why Isolation Forest if XGBoost wins on known patterns? | To catch patterns without labels. I chose its weight by leave-one-pattern-out, and E5 shows the effect. (§7.5-§7.6) |
| 15 | Are your explanations faithful? | Reason codes come from the model's own contributions, and a test checks they sum to the margin. (§7.10) |
| 16 | How did you measure latency? | Enqueue-to-scored timestamps at a paced rate below capacity. Latency under max load is mostly queueing. (§9.6) |
| 17 | How stale are graph features? | Measured per row from `graph_snapshot_ts`, and reported with the live-vs-offline metric comparison. (§6.5, §9.6) |
| 18 | How did you avoid tuning the generator to your metrics? | The config was frozen and hashed before modelling, with at most one documented realism revision. (§0.4, §4.8) |
| 19 | What are the limits of synthetic data? | Patterns I designed are easier than real fraud; there is one dataset and no confidence intervals. The README says so upfront. (§7.8) |
| 20 | What changes at 100x, or with synchronous authorization? | Kafka partitions, sharded or checkpointed state, a feature store, a separate low-latency authorization path, and a delayed-label retraining loop. (§9.7) |
| 21 | How is this different from your Delivery Delay project? | That one is about model lifecycle and promotion. This one is about real-time features, delivery guarantees and graph signals. (§0.1) |

---

## 22. Pre-start checklist

- [ ] VM runs Ubuntu 24.04 with 8-10 GB RAM and 6-8 vCPUs; the repo lives on the VM's own disk
- [ ] Docker Engine works without `sudo`; `docker compose version` reports v2
- [ ] uv is installed; Python 3.12 is available
- [ ] `claude --version` works in the Remote-SSH terminal
- [ ] GitHub repo exists; SSH authentication works
- [ ] `PLAN.md` is committed; `CLAUDE.md` is created from §18.2
- [ ] You agree with D1-D6 and A1-A14, or have edited §0.5
- [ ] 14 days are blocked out, with checkpoints on Days 5, 8 and 10

---

## Plan changelog

- **v1 (2026-09-16):** initial plan after the review.
  - Scoring mode: async stream monitoring.
  - Time budget: 14 days near full-time.
  - Refinements to the review: per-event atomic commit instead of sorted-set-only idempotency; `account_id` as the card identity.
