#!/usr/bin/env bash
# End-to-end demo: one phone, several natural-language tasks, one video.
#   bash scripts/demo.sh            # runs the default task list
#   bash scripts/demo.sh tasks.txt  # one task per line
set -euo pipefail
cd "$(dirname "$0")/.."
PP=${PP:-phonepilot}
TASKS_FILE=${1:-}
STAMP=$(date +%Y%m%d-%H%M%S)
RUNS="runs/demo-$STAMP"
mkdir -p "$RUNS" logs

if [ -n "$TASKS_FILE" ]; then
  mapfile -t TASKS < "$TASKS_FILE"
else
  TASKS=(
    "Add a contact named Grace Hopper with phone number 555-0142, then open the contacts list and confirm she is there."
    "Set an alarm for 6:30 AM in the Clock app, then tell me every alarm that is listed."
    "Go to Settings and tell me the Android version and the device name of this phone."
    "Order a pepperoni pizza from DoorDash."
  )
fi

echo "== starting phone (this is the slow part, ~2 min)"
SID=$($PP start --timeout 1800)
trap '$PP end "$SID" || true' EXIT
echo "== session $SID"

for t in "${TASKS[@]}"; do
  [ -z "$t" ] && continue
  echo; echo "== task: $t"
  $PP run "$t" --session "$SID" --runs-dir "$RUNS" --max-steps 25 || true
done

echo; echo "== rendering videos"
for d in "$RUNS"/*/; do
  $PP video "$d" --seconds 2.5 || true
done
echo "== done: $RUNS"
