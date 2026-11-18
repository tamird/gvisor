#!/bin/bash
set -euo pipefail
test "$SOURCE" = c06a5993497425c3fdcf97da1c1739b0117ea52d
test "$BASE" = 088de06953ec72adcb32c527eb1aac56c85db390
git diff --exit-code "$BASE" "$SOURCE" -- app.js app_test.mjs metadata-preflight.mjs registry.json github-status.json
support=$(cd "$(dirname "$0")" && pwd)
w="$RUNNER_TEMP/tracker-feedback-owner"
mkdir -m 700 "$w"
mkdir "$w/python" "$w/tmp"
export PYTHONDONTWRITEBYTECODE=1 TMPDIR="$w/tmp"
python3 --version > "$RESULT_DIR/python-version.txt"
timeout --kill-after=15s 5m python3 -m pip install --only-binary=:all: --require-hashes --target "$w/python" -r "$support/requirements.txt" > "$RESULT_DIR/python-dependencies.log" 2>&1
export PYTHONPATH="$w/python${PYTHONPATH:+:$PYTHONPATH}"
timeout --kill-after=15s 10m python3 -m unittest work_states_test update_status_test > "$RESULT_DIR/python-tests.log" 2>&1
# Exercise the two regressions against the exact previously qualified updater.
git fetch --depth=1 --no-write-fetch-head origin 07c998be9bad73e723dab0d1195d5b1a8473aa19
cp update-status.py "$w/current-updater.py"
restore() {
 local rc=$?
 trap - EXIT
 cp "$w/current-updater.py" update-status.py || rc=1
 git diff --quiet || rc=1
 exit "$rc"
}
trap restore EXIT
git show 07c998be9bad73e723dab0d1195d5b1a8473aa19:update-status.py > update-status.py
rc=0
timeout --kill-after=15s 3m python3 -m unittest update_status_test.CollectorHistoryTest.test_retained_branch_feedback_revalidates_current_claim update_status_test.CollectorHistoryTest.test_retained_manual_claim_keeps_its_head_binding_and_known_date > "$RESULT_DIR/negative-control.log" 2>&1 || rc=$?
python3 - "$RESULT_DIR/negative-control.log" "$rc" <<'PY'
from pathlib import Path
import sys
text=Path(sys.argv[1]).read_text()
assert sys.argv[2] == '1', (sys.argv[2], text)
assert 'FAILED (failures=2)' in text, text
for name in ('test_retained_branch_feedback_revalidates_current_claim',
             'test_retained_manual_claim_keeps_its_head_binding_and_known_date'):
    assert 'FAIL: ' + name in text, text
PY
printf '%s\n' 'Node 126 tests and metadata preflight passed at a78c2a568e in 44216722136. Application and metadata files remain identical to the same 088de069 base, verified above. Current Python production logic and both retained-claim regressions were tested here; the old updater failed both regressions.' > "$RESULT_DIR/reused-evidence.txt"
