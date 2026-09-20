# realtime-fraud-detection

Near-real-time transaction fraud monitoring on synthetic data: a simulated card-payment stream is
scored with behavioural, graph and anomaly signals, and every alert is explained.

> **Status: in progress.** The build plan is [`PLAN.md`](PLAN.md); current state is
> [`PROGRESS.md`](PROGRESS.md). This README is written in full at Phase 10, at which point every
> number in it will come from a committed script under `reports/`.

**Important:** the data is synthetic, produced by the simulator in this repository. This is
post-authorization monitoring — it does not block the payment it scores, it acts on what comes next.

## Quickstart

```bash
make setup      # uv sync --all-groups
make redis-up   # Redis via Docker Compose
make test
```
