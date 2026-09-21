# CV bullets

Filled from `reports/results.md` and `reports/benchmark.md` per PLAN §20.3. Every number
below is measured; none are targets. Re-derive them with `make evaluate-test V=v1` and
`make bench`.

---

## The bullets

**Real-Time Transaction Fraud Detection System** | Python, Redis Streams, NetworkX, XGBoost, FastAPI, DuckDB, Docker

Objective: Built a **streaming** fraud monitoring pipeline that scores card transactions using **behavioural**, **graph-based** and **anomaly** signals

- Simulated **494K** transactions with **4** injected fraud patterns (velocity abuse, account takeover, card testing, fraud rings) alongside legitimate look-alikes
- Engineered **36** leakage-safe features spanning **transaction velocity**, **spending deviation**, **device/IP fan-out**, **personalised PageRank** and **Louvain** community statistics
- Trained an **XGBoost + Isolation Forest** ensemble reaching **99.3% recall**, **0.975 F1** and **0.994 PR-AUC** on a time-split test set read exactly once; leave-one-pattern-out showed the anomaly half recovers **64-70% of an unseen fraud pattern** the supervised model scores near zero on
- Developed a **Redis Streams** consumer-group pipeline with idempotent processing, **FastAPI**, **DuckDB** analytics and native-contribution reason codes, sustaining **628 events/s** at **318 ms** p95 latency, with live output re-scored offline to **bit-identical** results

---

## Where each number comes from

| Placeholder | Value | Source |
|---|---|---|
| transactions | 494,189 → "494K" | `data/raw/manifest.json` |
| features | 36 | `src/fraud/features/spec.py` |
| recall | 0.993 → 99.3% | `reports/results.md`, E4 on test at the REVIEW operating point |
| F1 | 0.975 | same row |
| PR-AUC | 0.9938 → 0.994 | same row |
| LOPO recovery | 0.636 / 0.701 / 0.639 | `reports/experiments/E5.json` |
| throughput | 628 events/s | `reports/benchmark.md`, micro-batch 500 |
| p95 latency | 318 ms | `reports/benchmark.md`, 80% of capacity |

---

## Three deliberate departures from the §20.3 template

**"500K+" → "494K".** The simulator produces 494,189 events. "500K+" is false by 5,811,
and a number that rounds the wrong way is the easiest thing in a CV to be caught on.

**The ring-lift clause is dropped.** §20.3 offers "graph features lifted fraud-ring recall
from [a]% to [b]%", with the instruction to drop it if the lift is small. On test, E2 and
E3 both score **1.00** ring recall — the lift is exactly zero, because the behavioural
model already catches every ring. The clause is replaced with the leave-one-pattern-out
result, which is a real measurement rather than a missing one.

The honest version of the graph story, if asked: the six graph features take **50.1% of
total model importance** and `community_shared_devices` is the **single most important
feature of 36**; alone they catch **100% of rings at 100% precision and 0% of every other
pattern**. They are redundant on this dataset, not useless. See `reports/graph_ablation.md`.

**"SHAP reason codes" → "native-contribution reason codes".** The serving path uses
XGBoost's own `pred_contribs`, not the `shap` package (PLAN §3.8, A8): shap is a heavy
dependency that would explain only the XGBoost half of a blended score. `shap` is used
once, offline, for a figure. Claiming SHAP in production would be wrong in a way an
interviewer who knows the library would spot immediately.

---

## Two things to be ready to defend

**"Real-Time" in the title.** §20.3 permits it when p95 is well under a second and the
README says post-authorisation monitoring — both hold (318 ms; the README's first
sentence says "after authorisation"). If challenged, "near-real-time post-authorisation
monitoring" is the precise phrase, and conceding it immediately is better than defending
the marketing word.

**0.994 PR-AUC will draw scepticism, and should.** Lead with the caveat rather than
waiting for it: the data is synthetic, the patterns are ones I designed, and there is one
dataset with no confidence intervals. The interesting part is not the score — it is that
the first model scored **0.9995** and the honest response was to widen the simulator's
hard negatives once, which collapsed the rules baseline from 0.6305 to 0.4181 and is
documented in `reports/sim_realism_review.md`. That story is worth more than the metric.
