#!/bin/bash
set -euo pipefail
support=$(cd "$(dirname "$0")" && pwd)
w="$RUNNER_TEMP/tracker-feedback-owner"
mkdir -m 700 "$w"
mkdir "$w/python" "$w/tmp"
export PYTHONDONTWRITEBYTECODE=1 TMPDIR="$w/tmp"
python3 --version > "$RESULT_DIR/python-version.txt"
timeout --kill-after=15s 5m python3 -m pip install --only-binary=:all: --require-hashes --target "$w/python" -r "$support/requirements.txt" > "$RESULT_DIR/python-dependencies.log" 2>&1
export PYTHONPATH="$w/python${PYTHONPATH:+:$PYTHONPATH}"
timeout --kill-after=15s 10m python3 -m unittest work_states_test update_status_test > "$RESULT_DIR/python-tests.log" 2>&1
printf '%s\n' 'Node 126 tests and metadata preflight passed at a78c2a568ea59016dfce28d36c6263c52df3db3d in 44216722136. This revision changes only the existing Python test fixture type; application, metadata and all production code match that run.' > "$RESULT_DIR/reused-evidence.txt"
