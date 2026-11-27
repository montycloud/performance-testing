#!/usr/bin/env bash
# run_sequence.sh — runs mcperf.runners.users_sweep once per user-count in
# an explicit list, with a cooldown gap between each run, for
# scenarios/api/inventory_endpoint_load/locustfile.py.
#
# Thin wrapper only: does not touch config.yaml and does not re-implement the
# locust invocation itself — each step delegates to
# `python -m mcperf.runners.users_sweep --users <N>` (same tool any other
# scenario's sweep uses), so there is exactly one place that builds the
# locust command line. This script only adds the --gap cooldown sleep
# between steps, which users_sweep.py doesn't do (it's a single sequential
# sweep over 1..N or a list, back-to-back, with no pause).
#
# Run from the repo root so relative paths resolve.
#
# Examples
# --------
#   5 users, then 20, then 50, 10-minute cooldown between each, 5-minute runs:
#     ./scenarios/api/inventory_endpoint_load/run_sequence.sh \
#       --users 5,20,50 --run-time 5m --gap 10m --env stg1
#
#   Preview the commands without running anything:
#     ./scenarios/api/inventory_endpoint_load/run_sequence.sh \
#       --users 5,20,50 --run-time 5m --dry-run

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../../.." && pwd)"
LOCUSTFILE="$HERE/locustfile.py"

USERS=""
RUN_TIME="5m"
SPAWN_RATE=""
GAP="10m"
REPORTS_DIR="reports"
DRY_RUN=0
EXTRA_ARGS=()

usage() {
  sed -n '2,25p' "$0"
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --users) USERS="$2"; shift 2 ;;
    --run-time) RUN_TIME="$2"; shift 2 ;;
    --spawn-rate) SPAWN_RATE="$2"; shift 2 ;;
    --gap) GAP="$2"; shift 2 ;;
    --reports-dir) REPORTS_DIR="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) EXTRA_ARGS+=("$1"); shift ;;  # passed through to users_sweep.py / locust (e.g. --env, --mc-config)
  esac
done

if [ -z "$USERS" ]; then
  echo "ERROR: --users is required, e.g. --users 5,20,50" >&2
  exit 1
fi

to_seconds() {
  local val="$1"
  if [[ "$val" =~ ^([0-9]+)h$ ]]; then
    echo $(( ${BASH_REMATCH[1]} * 3600 ))
  elif [[ "$val" =~ ^([0-9]+)m$ ]]; then
    echo $(( ${BASH_REMATCH[1]} * 60 ))
  elif [[ "$val" =~ ^([0-9]+)s?$ ]]; then
    echo "${BASH_REMATCH[1]}"
  else
    echo "ERROR: cannot parse duration '$val' (expected e.g. 30m, 1h, 90s)" >&2
    exit 1
  fi
}

GAP_SECONDS="$(to_seconds "$GAP")"

cd "$REPO_ROOT"

IFS=',' read -ra USER_LIST <<< "$USERS"
first=1
for N in "${USER_LIST[@]}"; do
  if [ "$first" -eq 0 ]; then
    echo "[$(date)] Sleeping ${GAP} before next run..."
    if [ "$DRY_RUN" -eq 1 ]; then
      echo "  [dry-run] would sleep ${GAP_SECONDS}s"
    else
      sleep "$GAP_SECONDS"
    fi
  fi
  first=0

  rate="${SPAWN_RATE:-$N}"
  CMD=(python -m mcperf.runners.users_sweep
       --locustfile "$LOCUSTFILE"
       --reports-dir "$REPORTS_DIR"
       --users "$N"
       --spawn-rate "$rate"
       --run-time "$RUN_TIME")
  CMD+=("${EXTRA_ARGS[@]}")

  echo "[$(date)] Starting ${N}-user run"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] ${CMD[*]}"
  else
    # users_sweep.py's own exit code reflects run failures; don't let set -e
    # abort the whole sequence over one bad run — log it and continue.
    set +e
    "${CMD[@]}"
    RC=$?
    set -e
    if [ "$RC" -ne 0 ]; then
      echo "[$(date)] WARNING: ${N}-user run exited with code ${RC} - continuing to next user count"
    fi
  fi
done

echo "[$(date)] All runs complete."
