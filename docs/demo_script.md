# Demo recording — shot list

Target **2–3 minutes** (PLAN §17, Phase 10 task 2). Every shot below has been rehearsed on
this machine; the timings are measured, not estimated.

Record at **1280×720 or larger** — the dashboard is laid out for 1280 px.

---

## Before you hit record

```bash
make demo-reset      # tears the stack down, drops data/scored and data/graph/live
make up              # ~45 s to healthy; creates .env if missing
```

Wait for all five services to report healthy:

```bash
docker compose ps
```

Open these three tabs and leave them ready:

| Tab | URL |
|---|---|
| Dashboard | `http://localhost:8501` |
| API docs | `http://localhost:8000/docs` |
| Health | `http://localhost:8000/health` |

Have **two terminals** visible: one for the replayer, one for logs.

> The dashboard will show "No scored output yet" in every panel. That is correct and worth
> a beat on camera — it is the empty state, not a broken page.

---

## Shot list

### 0:00–0:20 — the stack

`docker compose ps`, then say what it is: redis, backfill (already exited 0), scorer,
graph-refresh, api, dashboard. One sentence on the ordering — **backfill runs to
completion before anything else starts**, because a scorer against cold state would treat
day 73 as if every account were new.

### 0:20–0:35 — start the replay

Terminal 1:

```bash
make demo            # replays the 18-day test window at 3600x
```

It prints `replaying 102,987 events`. Say the number and the compression: **one simulated
hour per real second**.

### 0:35–1:15 — the dashboard coming alive

Switch to the dashboard. Within ~10 s the panels populate. Point at, in order:

1. **System strip** — events/s, stream lag, p95 latency, DLQ 0, scorer heartbeat "live".
2. **Alert rate per simulated hour** — the line chart filling left to right.
3. **Replay scorecard** — and read the caption out loud: *"evaluation data the scorer never
   sees."* This is the one claim worth making explicitly. The stream carries no labels; the
   panel joins them after the fact.

### 1:15–1:35 — one alert, with its reasons

In **Recent alerts**, pick a row with a high risk and copy its `txn_id`. Paste it into
**Alert detail**.

Show `p_xgb`, the anomaly percentile, the blended risk, the decision — then the reasons
table. Read one aloud; they are sentences, not feature dumps:

> *"3 devices are shared inside this cluster"* · *"Account is 28 days old"* ·
> *"77% of linked accounts are under 30 days old"*

One line worth saying: these come from the model's own contributions, and a test asserts
they sum to the model's margin within 1e-4.

### 1:35–1:50 — the API

`http://localhost:8000/health` — model version, feature spec, latest snapshot, scorer
heartbeat age.

Then `/docs`. Expand **POST /score** and say the one thing that matters: **it never writes
state.** The stream is the single writer; an API that committed would double-count every
transaction it was asked about.

### 1:50–2:30 — the money shot: restart the scorer mid-replay

With the replay still running, terminal 2:

```bash
docker compose restart scorer
```

Measured: **0.73 s**. Then immediately:

```bash
docker compose logs --tail 20 scorer
```

Point at `stop requested; finishing the current batch and flushing`, then the new
`scorer scorer-1 ready: ... pending message(s) to replay`.

Say why it is safe: the scorer **acknowledges only after the Parquet flush**. An acked
message is gone from the pending list forever, so acking early would turn a crash into
silently missing output. Acking late costs at most a duplicate row, and readers deduplicate
on `txn_id`.

### 2:30–3:00 — the proof

When the replay finishes (terminal 1 prints `sent 102,987 events`), wait for the scorer to
drain — the dashboard's stream lag returns to 0 — then:

```bash
make rescore-check
```

Let the output sit on screen:

```
re-score        PASS  102,987 rows, max |diff| 0.00e+00 (tolerance 1e-09)
hot features    PASS  102,987 rows × 30 features, max |diff| 0.00e+00
graph parity    PASS  3 boundaries, max |diff| 0.00e+00
```

Close on the middle line: **the Redis state path and the in-memory state path agree
bit-for-bit across ~3 million feature values.** That is what the two-store design is for.

---

## Rehearsed results — what you should see

| Check | Measured on this machine |
|---|---|
| Stack healthy after `make up` | ~45 s |
| `docker compose restart scorer` | **0.73 s** |
| Rows after a mid-replay restart | **102,987 unique from 102,987 raw — 0 duplicates** |
| `make rescore-check` | `0.00e+00` on all three comparisons |
| Full replay wall time | ~7 min at 3600× |

**`docker compose restart` is graceful**: SIGTERM, then the scorer flushes and acks inside
`stop_grace_period: 20s`, so it produces *zero* duplicates. If you want to demonstrate the
harder path instead, `docker kill` the container — duplicates then become possible and the
DuckDB view is what removes them. The clean story films better; the crash story is the more
interesting answer if someone asks.

---

## Two things that will go wrong if you rush

**Recording before the stack is healthy.** The dashboard renders with empty panels and it
looks broken. Wait for `docker compose ps` to show `api ... (healthy)`.

**Restarting the scorer too early.** Restart after ~60–90 s of replay, so there is visible
progress on both sides of the event. Restarting in the first few seconds shows nothing.

---

## After recording

1. Upload, then add the link under the README title:

   ```markdown
   **[▶ 3-minute demo](YOUR_LINK_HERE)** — stack up, live replay, an alert with its
   reasons, and a scorer restart mid-replay with no duplicate rows.
   ```

2. Tick task 2 in `PROGRESS.md` → Phase 10 complete → **73/73**.
