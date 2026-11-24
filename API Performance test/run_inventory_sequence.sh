#!/usr/bin/env bash
# run_inventory_sequence.sh — orchestrates a sequence of inventory_locustfile.py
# runs at increasing user counts, with a cooldown gap between each run.
#
# Replaces the one-off hand-written bash commands from past sessions: each
# run's config.yaml (inventory: section only) is updated with the right
# run_mode, report_name and description "User count: N" line before Locust
# is invoked, so the generated HTML/CSV + custom report are correctly
# labeled without manual editing.
#
# This script does the test-running work in the FOREGROUND — it does not
# background/detach itself. Choose how to run it detached from your SSM/SSH
# session based on what you need:
#
#   Recommended — tmux (lets you reattach and watch Locust's live output):
#     tmux new -d -s inventory_test \
#       './run_inventory_sequence.sh --mode timed --users 10,20,50 --run-time 5m'
#     tmux attach -t inventory_test   # reattach any time
#     # detach again with Ctrl+b then d — the session keeps running
#
#   Fallback — nohup + setsid + disown (log-file only, no live reattach):
#     setsid nohup ./run_inventory_sequence.sh --mode timed --users 10,20,50 \
#       --run-time 5m > output.log 2>&1 < /dev/null &
#     disown
#     tail -f output.log
#
#   For recurring/scheduled runs, call this script from cron or a systemd
#   timer instead of either of the above.
#
# Examples
# --------
#   Single-journey (each user does exactly 1 iteration, no --run-time):
#     ./run_inventory_sequence.sh --mode single_journey --users 10,20,50
#
#   Timed (5-minute run per user count, half-spawn-rate ramp-up):
#     ./run_inventory_sequence.sh --mode timed --users 10,20,50 \
#       --run-time 5m --spawn-divisor 2
#
#   Preview the config edits + locust commands without running anything:
#     ./run_inventory_sequence.sh --mode timed --users 10,20,50 \
#       --run-time 5m --dry-run
#
# Run from inside the "API Performance test" directory so relative paths
# (inventory_locustfile.py, config.yaml, reports/) resolve correctly.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
MODE=""
USERS=""
RUN_TIME=""
GAP="30m"
SPAWN_DIVISOR=1
DATE_LABEL="$(date +%d%b)"
RUN_LABEL="run1"
DESC_NOTE="env QA with additional resources batch 1"
ENV_NAME="qa"
LOCUSTFILE="inventory_locustfile.py"
CONFIG_FILE="config.yaml"
REPORT_PREFIX="inventory_report"
LABEL_SUFFIX=""
DRY_RUN=0

usage() {
  sed -n '2,45p' "$0"
  exit 1
}

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --users) USERS="$2"; shift 2 ;;
    --run-time) RUN_TIME="$2"; shift 2 ;;
    --gap) GAP="$2"; shift 2 ;;
    --spawn-divisor) SPAWN_DIVISOR="$2"; shift 2 ;;
    --date) DATE_LABEL="$2"; shift 2 ;;
    --run-label) RUN_LABEL="$2"; shift 2 ;;
    --description-note) DESC_NOTE="$2"; shift 2 ;;
    --env) ENV_NAME="$2"; shift 2 ;;
    --locustfile) LOCUSTFILE="$2"; shift 2 ;;
    --config) CONFIG_FILE="$2"; shift 2 ;;
    --report-prefix) REPORT_PREFIX="$2"; shift 2 ;;
    --label-suffix) LABEL_SUFFIX="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

if [ "$MODE" != "single_journey" ] && [ "$MODE" != "timed" ]; then
  echo "ERROR: --mode must be 'single_journey' or 'timed' (got: '${MODE}')" >&2
  exit 1
fi
if [ -z "$USERS" ]; then
  echo "ERROR: --users is required, e.g. --users 10,20,50" >&2
  exit 1
fi
if [ "$MODE" = "timed" ] && [ -z "$RUN_TIME" ]; then
  echo "ERROR: --run-time is required when --mode timed, e.g. --run-time 5m" >&2
  exit 1
fi
if [ ! -f "$CONFIG_FILE" ]; then
  echo "ERROR: config file not found: $CONFIG_FILE (run this script from the 'API Performance test' directory, or pass --config)" >&2
  exit 1
fi
if [ ! -f "$LOCUSTFILE" ]; then
  echo "ERROR: locustfile not found: $LOCUSTFILE" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Convert "30m" / "45m" / "1h" / "90" (bare seconds) -> seconds.
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

# Human label for the description, e.g. "5m" -> "5 min", "1h" -> "1 hour".
human_duration() {
  local val="$1"
  if [[ "$val" =~ ^([0-9]+)h$ ]]; then
    echo "${BASH_REMATCH[1]} hour"
  elif [[ "$val" =~ ^([0-9]+)m$ ]]; then
    echo "${BASH_REMATCH[1]} min"
  else
    echo "$val"
  fi
}

# Set inventory.run_mode (scoped to the `inventory:` section only — never
# touches the other run_mode occurrences used by the chat/day2sdk sections).
set_run_mode() {
  local mode="$1"
  python3 - "$mode" "$CONFIG_FILE" <<'PYEOF'
import re, sys
mode, path = sys.argv[1], sys.argv[2]
with open(path) as f:
    lines = f.readlines()
start = next(i for i, l in enumerate(lines) if l.startswith("inventory:"))
end = next(i for i in range(start + 1, len(lines)) if re.match(r"^\S", lines[i]))
for i in range(start, end):
    if re.match(r"\s*run_mode:\s*\".*\"", lines[i]):
        indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        lines[i] = f'{indent}run_mode: "{mode}"\n'
with open(path, "w") as f:
    f.writelines(lines)
print(f"  config.yaml: inventory.run_mode -> {mode}")
PYEOF
}

# Set inventory.report_name + the description's "User count: N" line
# (scoped to the `inventory:` section only).
set_run_params() {
  local users="$1" report_name="$2" desc_note="$3" mode_label="$4"
  python3 - "$users" "$report_name" "$desc_note" "$mode_label" "$CONFIG_FILE" <<'PYEOF'
import re, sys
users, report_name, desc_note, mode_label, path = sys.argv[1:6]
with open(path) as f:
    lines = f.readlines()
start = next(i for i, l in enumerate(lines) if l.startswith("inventory:"))
end = next(i for i in range(start + 1, len(lines)) if re.match(r"^\S", lines[i]))
for i in range(start, end):
    if lines[i].strip().startswith("report_name:"):
        lines[i] = re.sub(r'report_name:\s*".*"', f'report_name: "{report_name}"', lines[i])
    if "User count:" in lines[i]:
        indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        lines[i] = f"{indent}User count: {users} , {desc_note} , {mode_label}\n"
with open(path, "w") as f:
    f.writelines(lines)
print(f"  config.yaml: User count -> {users} | report_name -> {report_name}")
PYEOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
GAP_SECONDS="$(to_seconds "$GAP")"

echo "[$(date)] Setting inventory.run_mode -> $MODE"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] would set run_mode -> $MODE"
else
  set_run_mode "$MODE"
fi

IFS=',' read -ra USER_LIST <<< "$USERS"

if [ "$MODE" = "timed" ]; then
  MODE_DESC_LABEL="Timed $(human_duration "$RUN_TIME") Run"
  AUTO_SUFFIX="_$(echo "$RUN_TIME" | sed -E 's/^([0-9]+)m$/\1mins/; s/^([0-9]+)h$/\1hr/')"
else
  MODE_DESC_LABEL="Single Journey"
  AUTO_SUFFIX=""
fi
SUFFIX="${LABEL_SUFFIX:-$AUTO_SUFFIX}"

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

  SPAWN_RATE=$(( N / SPAWN_DIVISOR ))
  [ "$SPAWN_RATE" -lt 1 ] && SPAWN_RATE=1
  REPORT_BASE="${REPORT_PREFIX}_${DATE_LABEL}_${RUN_LABEL}_${N}_users${SUFFIX}"

  echo "[$(date)] Preparing ${N}-user run (report: ${REPORT_BASE})"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] would set User count -> $N | report_name -> $REPORT_BASE"
  else
    set_run_params "$N" "$REPORT_BASE" "$DESC_NOTE" "$MODE_DESC_LABEL"
  fi

  CMD=(python3 -m locust -f "$LOCUSTFILE" --headless
       --users "$N" --spawn-rate "$SPAWN_RATE" --env "$ENV_NAME"
       --html "reports/${REPORT_BASE}.html"
       --csv "reports/${REPORT_BASE}")
  if [ "$MODE" = "timed" ]; then
    CMD+=(--run-time "$RUN_TIME")
  fi

  echo "[$(date)] Starting ${N}-user run"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] ${CMD[*]}"
  else
    # Locust exits non-zero (--exit-code-on-error, default 1) whenever the run
    # had any failed/errored request. Don't let `set -e` abort the whole
    # sequence over that — log it and move on to the next user count.
    set +e
    "${CMD[@]}"
    LOCUST_EXIT=$?
    set -e
    if [ "$LOCUST_EXIT" -ne 0 ]; then
      echo "[$(date)] WARNING: ${N}-user run exited with code ${LOCUST_EXIT} (likely test failures in results) - continuing to next user count"
    fi
  fi
done

echo "[$(date)] All runs complete."
