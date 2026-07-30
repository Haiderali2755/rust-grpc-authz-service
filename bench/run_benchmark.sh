#!/usr/bin/env bash
# End-to-end benchmark: builds the stack, seeds sessions, drives load, writes JSON.
set -euo pipefail

cd "$(dirname "$0")/.."

CONCURRENCY="${CONCURRENCY:-64}"
DURATION="${DURATION:-30}"
WARMUP="${WARMUP:-5}"

echo "==> Building and starting stack"
docker compose up -d --build

echo "==> Waiting for Redis"
for _ in $(seq 1 30); do
  if docker compose exec -T redis redis-cli ping >/dev/null 2>&1; then break; fi
  sleep 1
done

# Seed one session per worker. Without this every Check would return
# DENY_UNAUTHENTICATED and the benchmark would measure the early-return path
# rather than the full resolve-plus-rate-limit path.
echo "==> Seeding $CONCURRENCY sessions"
{
  for i in $(seq 0 $((CONCURRENCY - 1))); do
    echo "SET session:tok-$i subject-$i"
  done
} | docker compose exec -T redis redis-cli --pipe >/dev/null 2>&1 || {
  for i in $(seq 0 $((CONCURRENCY - 1))); do
    docker compose exec -T redis redis-cli SET "session:tok-$i" "subject-$i" >/dev/null
  done
}

echo "==> Waiting for authz service"
for _ in $(seq 1 60); do
  if docker compose logs authz 2>/dev/null | grep -q "listening"; then
    echo "    ready"
    break
  fi
  sleep 1
done

# Clear rate-limit counters so the measured window starts clean.
docker compose exec -T redis redis-cli --scan --pattern 'rl:*' 2>/dev/null \
  | xargs -r -n 100 docker compose exec -T redis redis-cli DEL >/dev/null 2>&1 || true

echo "==> Running load generator (concurrency $CONCURRENCY, ${DURATION}s)"
mkdir -p bench/results
docker run --rm --network host \
  -v "$PWD":/work -w /work/bench/loadgen \
  -e TARGET=http://127.0.0.1:50051 \
  -e CONCURRENCY="$CONCURRENCY" \
  -e DURATION="$DURATION" \
  -e WARMUP="$WARMUP" \
  -e OUT=/work/bench/results/latency.json \
  -e CARGO_TARGET_DIR=/work/bench/loadgen/target \
  rust:1.88-slim \
  sh -c 'apt-get update >/dev/null && apt-get install -y --no-install-recommends protobuf-compiler >/dev/null && cargo run --release'

echo
echo "Done. Raw measurements: bench/results/latency.json"
echo "Render the page with: python3 bench/render_chart.py"
