# Interview notes

The §21 questions, answered with this project's measured numbers. Not linked from the
README — this is preparation, not documentation.

The habit worth keeping across all of these: **lead with the number, then the reason, then
the limitation.** Most candidates give the reason and stop.

---

### 1. Why not accuracy?

Fraud is **1.69%** of this dataset, so "always legitimate" scores 98.3% accuracy and
catches nothing. I report PR-AUC as the headline (**0.994** on test) and then precision,
recall and FPR at an operating point constrained to a **2% alert budget**, because a
threshold that alerts on 20% of traffic is not a threshold anyone can staff.

The budget is the honest part. Without it, F1 happily picks a threshold no team could work.

### 2. What does HOLD do if the payment already went through?

It freezes the card or account for what comes *next* — a 7-day `hold:acct:{id}` key. That
is how card testing and takeover are contained: the first probe gets through, the next
forty do not.

**On this dataset HOLD is disabled**, and that is the more interesting answer. §7.7 picks
the HOLD threshold as the lowest reaching 0.95 precision, which assumes precision at the
alert budget sits below that bar. Mine is 0.960, so the whole alert set qualifies and HOLD
would swallow every alert — freezing every flagged customer and leaving no analyst queue.
I disabled the tier and reported it rather than moving the bar until two tiers appeared.

### 3. How do you prevent leakage in windowed features?

Four mechanics, each with a test:
- **Event time only**, never `time.time()` — otherwise a replay cannot reproduce a live run.
- **Half-open windows `[t - w, t)`** — an inclusive upper bound lets an event count itself.
- **Compute features, then update state.** Reversed, the event leaks into its own features.
- **Time-based splits** with a 14-day burn-in dropped, because partly-filled windows teach
  the model that a cold account is normal.

Ten leakage rules (L1-L10) with a test each. L10 is the one with teeth: `splits.load("test")`
raises unless `ALLOW_TEST=1`, and `reports/test_runs.log` has exactly one line.

### 4. Why was your first graph's clustering coefficient zero?

An account→device/IP graph is **bipartite**, and bipartite graphs have no triangles, so
clustering is identically zero. It is not a bug you debug, it is a modelling error — the
feature was structurally incapable of carrying information.

Fixed by projecting to **account↔account**: two accounts share an edge if they shared a
device or IP. Triangles then mean something — three accounts on one device.

### 5. Why cap shared devices and IPs?

Because carrier NAT, offices and card-testing devices link people who have nothing to do
with each other. I measured it on train only: the median accounts-per-IP for carrier NAT
is **114**, VPN reaches 58, offices 27. Without a cap those fuse hundreds of unrelated
victims into one fake "ring", and personalised PageRank then spreads fraud risk to all of
them.

Caps: **device 16, IP 18**. §6.1 also suggests the 99.5th percentile, which on this data is
**4** for devices — that would erase the rings the graph exists to find, since a ring puts
6-15 accounts on a device. 16 sits above rings (max 15) and legitimate shared devices
(max 14) and below card-testing devices (p90 42).

### 6. Why personalised PageRank with a label delay?

It measures proximity to *known* fraud, which is the online question. The **14-day delay**
mirrors chargeback timing: a seed may only be an account whose `label_available_at` is
before the snapshot. Without it the feature knows about fraud that had not been discovered
yet and is optimistic in a way that never survives production.

Detail worth having ready: `ppr_risk` is ~1e-5 rather than exactly 0 for unreachable
accounts, because power iteration starts from a uniform vector. Five orders of magnitude
below seeded values, so it cannot move a split.

### 7. Why not ASOF-join graph features on `account_id`?

An ASOF join on account alone carries an account's **last known** values forward forever.
An account that dropped out of later snapshots — because the fan-out caps excluded it, or
it stopped transacting — keeps a graph position it no longer has, and the model learns from
a position that is not real.

Two-step instead: resolve the applicable snapshot from the event time, *then* left-join
that account in *that* snapshot, taking §6.3 defaults when absent. An account that left the
graph gets defaults, which is the truth.

### 8. How do offline and online features match?

One `FeatureEngine`, two stores. A parity test runs both over the same sequences and
compares all 30 hot features. Then `make rescore-check` re-scores the live output offline:

```
hot features    PASS  102,987 rows × 30 features, max |diff| 0.00e+00
```

Bit-identical across ~3 million feature values. That is the claim I would open with — it is
stronger than "we have tests".

### 9. The scorer crashes after committing state but before acking. What happens?

The message is redelivered, the feature engine returns the **stored record** (same
features, same graph snapshot), an identical row is written a second time, and the DuckDB
reader deduplicates on `txn_id`.

Every row of the §9.3 failure matrix has a test driven by a `crash_after` hook. The one I
like most publishes a *newer* snapshot in the gap between the crash and the retry, and
asserts both written rows still carry the original — otherwise a retry would score
differently from the row already on disk.

### 10. Why acknowledge after the flush?

An acked message leaves the pending list **forever**; nothing will ever redeliver it. So
acking before the row is durable converts any crash into silently missing output that no
downstream check can detect. Acking after costs at most a duplicate row, which readers
already resolve.

**A visible duplicate beats an invisible hole.** That is the whole trade in one sentence.

### 11. How is ordering preserved? How would you scale out?

One scorer, consuming in stream order. The replayer sends in `(event_time, txn_id)` order —
`txn_id` matters because the window has **5,260 tied timestamps** and "sorted by time"
leaves their order undefined, which would make the re-score check compare two legitimately
different sequences.

To scale: partition by `account_id` (ordering holds per account, which is all the features
need), one scorer per partition, and the graph job must wait for the **minimum** watermark
across partitions before publishing a snapshot.

### 12. Why Redis Streams and not Kafka?

The same concepts — consumer groups, offsets, at-least-once, pending/claim — in one
container, and it doubles as the state store, so features and the log are one round trip
apart. Kafka is the right answer at higher volume, and I would move to it for partitioning,
retention and replication, not because Streams got the semantics wrong.

The honest limitation: Redis AOF at `everysec` can lose ~1 s of the stream tail on restart.
Those events are never scored. A replicated log fixes it; I documented it instead.

### 13. Why Parquet + DuckDB?

A DuckDB **file** allows one read-write process or many readers, never both — so the
dashboard holding it would block the scorer's next write. Single-writer Parquet with
**in-memory** DuckDB readers over a glob has no file and no lock, so every reader is
independent and the scorer never contends.

The view also deduplicates (`QUALIFY row_number() OVER (PARTITION BY txn_id ORDER BY
scored_ts) = 1`), which is what makes the crash-before-ack design affordable.

### 14. Why Isolation Forest if XGBoost wins on known patterns?

Because XGBoost knows exactly the four patterns it was shown. The Isolation Forest is for
the fifth.

I chose its weight by **leave-one-pattern-out**, not by maximising validation — validation
only holds patterns the model was trained on, so that search drives `w` to 1.0 and proves
only that a supervised model beats an unsupervised one at its own job. Withholding each
pattern in turn:

| Withheld | XGB alone | Blended at w*=0.5 |
|---|---|---|
| VELOCITY | 0.049 | **0.636** |
| ATO | 0.092 | **0.701** |
| CARD_TESTING | 0.005 | **0.639** |
| RING | 0.000 | **0.000** |

Two-thirds of an unseen pattern recovered, for 6-13 points of precision. **RING recovers
nothing** — rings are behaviourally ordinary, so there is nothing to isolate, and only the
graph finds them. That is the strongest argument for the graph layer in the project.

Caveat to volunteer: `w* = 0.5` sits at the edge of the plan's grid and beats `w = 0.7` by
about two transactions. The flat top is noise; the step away from `w = 1.0` is not.

### 15. Are your explanations faithful?

They come from the model's own `pred_contribs`, and a test asserts they sum to the model
margin within 1e-4 — measured **2.0e-05** on 5,000 real rows. If that ever fails, the
reasons describe a different model than the one deciding, which is worse than no reasons.

Only **positive** contributions become reasons: a feature arguing for innocence is not why
something was flagged. And `shap` is deliberately not in the serving path — heavy, and it
would explain only the XGBoost half of a blended score.

### 16. How did you measure latency?

`ingest_ts` (stamped at XADD) to `scored_ts`, at a **paced** rate below capacity: p50 154 ms
and p99 224 ms at half capacity, p95 318 ms at 80%.

Never under `--max` — there it is mostly queueing time. I know that because I produced the
bad number first: my first harness replayed everything *before* starting the scorer and
reported a p50 of **45 seconds**. Then, after fixing that, p95 was worse at 50% load than at
80%, which has no physical explanation; measuring the same config in isolation gave 225 ms
flat across all deciles. The tail was the benchmark's own disk traffic. The harness now
settles 10 s between runs and the report states it.

### 17. How stale are graph features?

Every scored row stores `graph_snapshot_ts`, so it is measured per row rather than
estimated. On the full replay with graph-refresh running concurrently: **p50 17.1 h, p95
23.5 h**, and 102,987/102,987 rows resolved a snapshot. Snapshots are daily, so roughly
half a day is the floor.

Resolution is a bisect for the latest snapshot **at or before** the event time. Taking the
newest would hand an event graph features built from its own future.

### 18. How did you avoid tuning the generator to your metrics?

`configs/sim.yaml` was frozen and SHA-256 hashed before any modelling, and the hash is
pinned in `CLAUDE.md` and written into every run manifest and model artifact.

§4.8 allows exactly **one** realism revision, and I used it: E2 first scored PR-AUC 0.9995,
which crossed the plan's "suspiciously perfect" line. I investigated before reporting and
established it was not leakage — best single-feature PR-AUC was 0.53 — but zero overlap:
**0 of 65,604** legitimate rows had `dev_accts_30d >= 5`. `sim-v2` widened the hard
negatives to 866, and the rules baseline collapsed from 0.6305 to **0.4181**. There is no
sim-v3.

### 19. What are the limits of synthetic data?

Patterns I designed are easier than real fraud — I know where they are because I put them
there. One dataset, no confidence intervals, and rows within an attack are correlated so a
row-level bootstrap would overstate certainty. The README says this on the first screen.

What the project *does* demonstrate is the engineering: exactly-once state under
at-least-once delivery, offline/online parity to the bit, and point-in-time correctness.
Those claims do not depend on the data being real.

### 20. What changes at 100×, or with synchronous authorisation?

Kafka partitioned by account; sharded Redis or checkpointed state in Flink; a feature store
to manage offline/online parity instead of a parity test; a **separate** synchronous
authorisation path with a tens-of-milliseconds budget reading the same online features; and
a delayed-label pipeline feeding retraining and drift monitoring.

Synchronous authorisation is the bigger change: 318 ms p95 is fine for monitoring and far
too slow for an auth decision. That path would drop the graph lookup and the Isolation
Forest and serve a much smaller model.

### 21. How is this different from your Delivery Delay project?

That one is about model lifecycle — training, registry, promotion, MLflow. This one is
about **delivery guarantees and time correctness**: what happens when a consumer dies
mid-batch, how a feature computed online proves equal to one computed offline, and how a
graph feature avoids knowing the future.

Different failure modes, so different tools: a versioned folder with startup compatibility
checks here instead of a registry, because there is one model and no promotion flow.

---

## If I had another week

- Re-run the graph ablation on a dataset where rings are behaviourally ordinary, which is
  the condition under which E3 would actually beat E2.
- Partitioned streams (K = 2-4) and a scaling curve, to make the §9.7 answer measured.
- Confidence intervals via an attack-level bootstrap, since row-level would be wrong.
- The slim serving image; dropping xgboost's CUDA libraries already took 250 MB out.
