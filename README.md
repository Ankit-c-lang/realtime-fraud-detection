# Realtime fraud detection

> **[▶ Demo recording](docs/demo_script.md)** — *link to be added after recording; the
> shot list and rehearsed timings are in `docs/demo_script.md`.*

Near-real-time monitoring of card transactions **after** authorisation: a Redis Streams
pipeline scores every payment against a behavioural model, a graph model and an anomaly
detector, and routes it to `ALLOW`, `REVIEW` or `HOLD` within an analyst capacity budget.
The full 18-day evaluation window replays end to end in about seven minutes and every
scored row is reproducible offline to the last bit.

> **The data is synthetic.** A simulator I wrote generates the accounts, merchants,
> devices and four fraud patterns (`configs/sim.yaml`, frozen). Patterns I designed are
> easier to find than real fraud, there is one dataset, and there are no confidence
> intervals. Treat the numbers below as evidence that the *system* works, not as a claim
> about real-world fraud detection. Every one of them is generated into `reports/` by a
> command shown here — none are typed by hand.

---

## Architecture

```
                    ┌──────────────┐
  sim.yaml ───────► │  simulator   │ ──► data/raw/*.parquet   494,189 events, 1.69% fraud
   (frozen)         └──────────────┘            │
                                                ▼
                    ┌──────────────┐    ┌────────────────┐
                    │ FeatureEngine│◄───┤ offline replay │ ──► hot_features.parquet
                    │  (30 hot)    │    └────────────────┘     + state checkpoint
                    └──────┬───────┘            │
                           │            ┌───────▼────────┐
                           │            │ graph snapshots│ ──► 6 warm features
                           │            │  (daily, PIT)  │
                           │            └───────┬────────┘
                           │                    ▼
                           │            ┌────────────────┐
                           │            │ XGBoost + iForest ──► models/v1  (blend w*=0.5)
                           │            └───────┬────────┘
   ════════════════════════╪════════════════════╪═══════════════ offline │ online ═══
                           │                    │
   replayer ──► txn:events │                    ▼
   (3600×)         │       └──► scorer ──► RiskModel ──► data/scored/*.parquet
                   │              │  ▲                        │
                   │              │  └── graph:{T}:acct:{id}  │
                   │              ▼         ▲                 ▼
                   └────────► Redis ────────┘            DuckDB view
                          (state, streams)                │      │
                                                     FastAPI   Streamlit
```

**The same `FeatureEngine` runs offline and online** — only the store differs
(`InMemoryStore` vs `RedisStore`). That is what makes the parity check below meaningful
rather than a comparison of two implementations.

---

## Quickstart

```bash
make setup          # uv sync, all dependency groups
make all            # simulate → features → graph → train  (~12 min, CPU only)
make up             # docker compose: redis, backfill, scorer, graph-refresh, api, dashboard
make demo           # replay the 18-day test window at 3600× (~7 min)
make rescore-check  # prove the live output reproduces offline, exactly
```

Then open the dashboard at `localhost:8501` and the API docs at `localhost:8000/docs`.

`make smoke` runs a bounded version of the same thing (2,000 events) and asserts the row
count and a healthy `/health`.

---

## Results

Test split, read **once**, on 102,987 events (`reports/results.md`, `reports/test_runs.log`).

| Exp | Model | PR-AUC | Precision | Recall | FPR | Alert rate | VEL | ATO | CT | RING |
|---|---|---|---|---|---|---|---|---|---|---|
| E1 | Rules R1–R4 | 0.4799 | 0.702 | 0.663 | 0.0058 | 1.90% | 0.81 | 0.16 | 0.77 | 0.64 |
| E2 | XGBoost, 30 hot features | 0.9982 | 0.960 | 0.993 | 0.0008 | 2.08% | 0.96 | 1.00 | 1.00 | 1.00 |
| E3 | XGBoost, 36 (+ graph) | 0.9983 | 0.953 | 0.994 | 0.0010 | 2.10% | 0.96 | 1.00 | 1.00 | 1.00 |
| **E4** | **E3 + Isolation Forest at w\*=0.5** | 0.9938 | 0.958 | 0.993 | 0.0009 | 2.09% | 0.97 | 1.00 | 1.00 | 0.99 |

Validation PR-AUC for E4 is 0.9938 against 0.9938 on test — the model did not overfit the
split it was tuned on.

### Three results I did not get the way I wanted

**The graph ablation shows no gain, and that is the reported result.** E2 already reaches
ring recall 1.00, so E3 has no headroom and matches it to four decimals. What the ablation
*can* establish is reported instead: the six graph features take **50.1% of total model
importance**, `community_shared_devices` is the **single most important feature of 36**,
and alone (E3b) they catch **100% of rings at 100% precision and 0% of every other
pattern**. On this dataset they are **redundant, not useless** — and the condition under
which they would separate is a dataset where rings are behaviourally ordinary, which is
what a real mule network looks like. (`reports/graph_ablation.md`)

**The HOLD tier is disabled.** §7.7 picks the HOLD threshold as the lowest reaching 0.95
precision, which assumes precision at the alert budget sits *below* that bar. Here it is
0.960, so the whole alert set qualifies and HOLD would swallow every alert — freezing
every flagged customer and leaving no analyst queue. The tier is switched off and reported
as not separating, rather than manufactured by moving the bar.

**The test alert rate is 2.09%, above the 2% budget.** The threshold was frozen on
validation (1.37% there) and deliberately not re-tuned on test. The cause is prevalence,
not drift: the test window carries 2.01% fraud against validation's 1.34%, and at 99.3%
recall the alert count tracks the fraud count. E2 and E3 overshoot by the same margin.

### Does the ensemble earn its place? (E5)

Each pattern is withheld from training in turn, so `XGB_-k` has genuinely never seen it.
Recall **on the withheld pattern**, alone versus blended at `w*`:

| Withheld | Alone | Blended | Overall precision |
|---|---|---|---|
| VELOCITY | 0.049 | **0.636** | 0.976 → 0.909 |
| ATO | 0.092 | **0.701** | 0.996 → 0.939 |
| CARD_TESTING | 0.005 | **0.639** | 0.950 → 0.818 |
| RING | 0.000 | **0.000** | 0.928 → 0.934 |

The anomaly half recovers roughly two-thirds of a pattern the supervised model is blind
to, for 6–13 points of precision. **RING recovers nothing** — rings are behaviourally
ordinary, so there is nothing to isolate, and only the graph structure finds them. That is
the clearest argument for the graph layer in the whole project.

`w*` is chosen by leave-one-pattern-out rather than by maximising validation performance,
because validation only holds patterns the model was trained on and would drive `w` to 1.0.

---

## Benchmark

`reports/benchmark.md`, generated by `make bench`. i5-1235U, 10 logical CPUs, 7.7 GB RAM,
Redis 8.10.1, Python 3.12.3.

| Micro-batch | 1 | 50 | 200 | 500 |
|---|---|---|---|---|
| events/s | 33 | 437 | 588 | **628** |

| Load | Rate | p50 | p95 | p99 |
|---|---|---|---|---|
| 50% of capacity | 314/s | 154 ms | 210 ms | 224 ms |
| 80% of capacity | 503/s | 214 ms | 318 ms | 347 ms |

Latency is measured at a fixed rate, never under `--max`, where it would be queueing time
rather than the system's. Peak container memory during a Compose replay: graph-refresh
529.5 MiB, replayer 322.4 MiB, api 224.1 MiB, scorer 223.1 MiB, redis 56.17 MiB,
dashboard 50.44 MiB.

### Correctness: the live path reproduces the offline path exactly

`make rescore-check` on the full 102,987-row replay:

```
re-score        PASS  102,987 rows, max |diff| 0.00e+00 (tolerance 1e-09)
hot features    PASS  102,987 rows × 30 features, max |diff| 0.00e+00
graph parity    PASS  3 boundaries, max |diff| 0.00e+00
```

Bit-identical, not merely inside tolerance. The middle line is the one that matters: the
Redis state path and the in-memory state path agree across ~3 million feature values.
Live metrics match the recorded offline E4 exactly on every operating-point measure;
PR-AUC differs by 9.75e-06, which is tie ordering inside `average_precision_score`.

---

## Design decisions

| Decision | Why |
|---|---|
| Redis Streams, not Kafka | Consumer groups, pending lists and `XAUTOCLAIM` give at-least-once semantics on one VM. Kafka's partitioning is what I'd want at 100×, not here. |
| One `FeatureEngine`, two stores | A second implementation for serving is how offline and online drift, silently. The parity test exists because that drift is invisible. |
| Event time only, half-open windows `[t-w, t)` | A window measured against wall-clock cannot be replayed; an inclusive upper bound lets an event count itself. |
| Compute features **before** updating state | Otherwise the event leaks into its own features. |
| Graph projected account→account, with fan-out caps | Clustering is 0 on a bipartite graph. Caps keep rings (6–15 accounts) and drop carrier NAT (median 114 accounts/IP). |
| Personalised PageRank with a 14-day label delay | Seeds must be fraud that would have been *known* at snapshot time, not fraud discovered later. |
| Two-step point-in-time join | An ASOF join on `account_id` alone carries an account's last-known values forward forever. |
| No `scale_pos_weight` | The probability is blended with an anomaly percentile; reweighting distorts it and the blend weight stops meaning anything. Imbalance is handled at the threshold. |
| Versioned model folder, not MLflow | One model, no promotion workflow. A folder plus `metadata.json` plus startup compatibility checks is the honest minimum. |
| `XACK` only after the Parquet flush | An acked message is gone forever; acking early turns a crash into silently missing output. |
| `/score` never writes state | The stream is the single writer. An API that committed would double-count every transaction it was asked about. |

---

## Failure handling

Delivery is at-least-once; the effect on state is exactly-once. Every crash point has a
test (`tests/test_scorer_recovery.py`, driven by a `crash_after` hook).

| Crash point | Redis state | Parquet output | On restart |
|---|---|---|---|
| Before the feature commit | unchanged | none | message still pending, processed normally |
| After commit, before flush | updated once | none | pending message returns the stored record → row written **once** |
| After flush, before `XACK` | updated once | row written | identical row written again; readers deduplicate on `txn_id` |
| Poison message | unchanged | DLQ entry | acknowledged after the DLQ write |
| Redis restart (AOF `everysec`) | up to ~1 s of writes lost | unaffected | **known limitation** — see below |

A redelivered event returns its stored feature record, including the graph snapshot chosen
the first time, so a retry cannot score differently from the row already on disk.

---

## Limitations

- **Synthetic data.** One dataset, no confidence intervals, and patterns I designed. Rows
  within one attack are correlated, so a naive row-level bootstrap would overstate certainty.
- **The simulator was revised once.** E2 first scored PR-AUC 0.9995 because legitimate
  traffic had literally zero overlap with three fraud shapes. `sim-v2` widened the hard
  negatives and E1 collapsed from 0.6305 to 0.4181. The revision is spent; there is no
  sim-v3, and the reasoning is in `reports/sim_realism_review.md`.
- **HOLD is disabled** on this dataset (above).
- **A Redis restart can lose ~1 s of the stream tail.** Those events are never scored. The
  production answer is a replicated log and replicated state.
- **`/score` is not point-in-time for old events.** Scoring a transaction older than the
  account's last processed event yields a negative `secs_since_last`, which the model never
  saw in training. This is detected and logged.
- **Graph staleness.** The live system may use snapshot T-1 while T is computing; every
  scored row records `graph_snapshot_ts` and the re-score check reports the distribution.

---

## What I'd change at 100× scale

- **Log:** Kafka partitioned by `account_id` — ordering holds per account, parallelism
  grows with partitions, and you get retention and replication.
- **State:** shard Redis by account, or move to a checkpointed stream processor (Flink).
- **Concurrency:** if two consumers could process one message at once, the idempotency
  check and the commit must become a single Lua script. Here one scorer makes that
  unnecessary, which is why it is not built.
- **Graph:** simple entity-link counts in the stream; heavy graph jobs on a cluster.
  Streaming community detection is research territory, not an afternoon.
- **Partitioned streams** need the graph job to use the **minimum** watermark across
  partitions before publishing a snapshot.
- **Monitoring:** alerting on consumer lag, DLQ growth and latency SLOs.

---

## Environment

Ubuntu 24.04 · Python 3.12.3 · uv 0.12.17 · Docker Engine 29.8.0 (rootless) · Redis
8.10.1 · i5-1235U (10 logical CPUs) · 7.7 GB RAM · **CPU only, no GPU**.

Training runs in the host virtualenv; the image only serves. CI runs three jobs: lint +
555 unit tests, 150 Redis integration tests against the same Redis configuration Compose
uses, and an image build that asserts the package imports and that dev dependencies did
not leak in.

## Repository map

| Path | What |
|---|---|
| `src/fraud/sim/` | the simulator (frozen at `sim-v2-fix1`) |
| `src/fraud/features/` | `spec.py` (36 features), the engine, both stores |
| `src/fraud/graph/` | projection, algorithms, daily snapshots, point-in-time join |
| `src/fraud/modeling/` | splits, metrics, rules, XGBoost, Isolation Forest, blend, decisions, artifacts |
| `src/fraud/stream/` | replayer, scorer, sink, backfill, recovery |
| `src/fraud/api/`, `dashboard/` | FastAPI service and Streamlit dashboard |
| `reports/` | every published number, generated |
| `PLAN.md` | the full design this was built from |
| `PROGRESS.md` | what was built when, and every deviation from the plan with its reason |
