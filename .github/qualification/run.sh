#!/bin/bash
set -euo pipefail
if test "${TIMESTAMP_DEADLINE_ACTIVE:-}" != 1; then
  exec timeout --signal=TERM --kill-after=60s 20m env TIMESTAMP_DEADLINE_ACTIVE=1 bash "${BASH_SOURCE[0]}" "$@"
fi
test "$SOURCE" = 292402566170e3e905fcd9e8d5f522eaad61b204
test "$BASE" = 50d61afa6c03c9cead5c64eefac06401dc6f432f
git diff --exit-code "$BASE" "$SOURCE" -- '*.py' registry.json github-status.json metadata-preflight.mjs README.md
support=$(cd "$(dirname "$0")" && pwd)
w="$RUNNER_TEMP/tracker-branch-timestamp"
mkdir -m 700 "$w"
mkdir "$w/python" "$w/tmp"
export PYTHONDONTWRITEBYTECODE=1 TMPDIR="$w/tmp"
python3 --version > "$RESULT_DIR/python-version.txt"
node --version > "$RESULT_DIR/node-version.txt"
timeout --kill-after=15s 5m python3 -m pip install --only-binary=:all: --require-hashes --target "$w/python" -r "$support/requirements.txt" > "$RESULT_DIR/python-dependencies.log" 2>&1
export PYTHONPATH="$w/python${PYTHONPATH:+:$PYTHONPATH}"
timeout --kill-after=15s 10m node --test --test-reporter=tap app_test.mjs > "$RESULT_DIR/node-tests.log" 2>&1
timeout --kill-after=15s 1m node metadata-preflight.mjs . > "$RESULT_DIR/metadata-preflight.json"
cp app.js "$w/current-app.js"
restore() {
 local rc=$?
 trap - EXIT
 timeout --kill-after=5s 10s cp "$w/current-app.js" app.js || rc=1
 timeout --kill-after=5s 10s git diff --quiet --exit-code || rc=1
 exit "$rc"
}
trap restore EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
git show "$BASE":app.js > app.js
rc=0
timeout --kill-after=15s 3m node --test --test-reporter=tap --test-name-pattern='registry requires an aware update timestamp for collected branches' app_test.mjs > "$RESULT_DIR/negative-control.log" 2>&1 || rc=$?
python3 - "$RESULT_DIR/negative-control.log" "$rc" <<'PY'
from pathlib import Path
import sys
text=Path(sys.argv[1]).read_text()
assert sys.argv[2] == '1', (sys.argv[2], text)
assert 'not ok' in text and 'registry requires an aware update timestamp for collected branches' in text, text
assert 'Missing expected exception' in text and '# fail 1' in text, text
PY
printf '%s\n' 'Python production/test blobs are unchanged from the source-bound163-test result44223150269. Node tests and exact old-validator negative control ran here; no old Python test failure is being hidden.' > "$RESULT_DIR/reused-evidence.txt"
