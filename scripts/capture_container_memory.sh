#!/usr/bin/env bash
# Peak container memory during a Compose replay (PLAN §12.2 "Resources").
#
# `make bench` runs the scorer in-process, so `docker stats` there would report idle
# containers and describe the wrong thing entirely. This replays through the running
# stack and samples the real services.
set -euo pipefail

EVENTS="${EVENTS:-15000}"
SAMPLES="${SAMPLES:-24}"
OUT="reports/container_memory.tsv"
RAW="$(mktemp)"
trap 'rm -f "$RAW"' EXIT

docker compose start scorer graph-refresh >/dev/null 2>&1 || true
sleep 8

# Sample in the background while the replay runs, so the peak is caught in flight
# rather than after everything has settled back down.
(
  for _ in $(seq 1 "$SAMPLES"); do
    docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}' 2>/dev/null || true
    sleep 2
  done
) > "$RAW" &
SAMPLER=$!

docker compose run --rm --no-deps replayer \
  python -m fraud.stream.replayer --max --limit "$EVENTS"
wait "$SAMPLER"

# Highest sample per service; the one-off `replayer-run-<hash>` container is normalised
# so the table does not change name on every run.
awk -F'\t' '{split($2,m," / "); gsub(/realtime-fraud-detection-|-1$/,"",$1);
             if ($1 ~ /^replayer-run/) $1="replayer"; print $1"\t"m[1]}' "$RAW" \
  | sort -k1,1 -k2,2hr | awk '!seen[$1]++' | sort > "$OUT"

echo "wrote $OUT"
cat "$OUT"
