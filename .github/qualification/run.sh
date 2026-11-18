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
timeout --kill-after=15s 4m curl --proto '=https' --proto-redir '=https' -fsSL --retry 2 --max-time 180 https://nodejs.org/dist/v26.13.1/node-v26.13.1-linux-x64.tar.xz -o "$w/node.tar.xz"
printf '%s  %s\n' 5c063e46bab0fbc1c17cc445d7ff129cbdcd0471be6ed83b6ab864f8143bc864 "$w/node.tar.xz" | sha256sum -c -
tar -xf "$w/node.tar.xz" -C "$w"
export PATH="$w/node-v26.13.1-linux-x64/bin:$PATH"
node --version > "$RESULT_DIR/node-version.txt"
status=0
timeout --kill-after=15s 10m python3 -m unittest work_states_test update_status_test > "$RESULT_DIR/python-tests.log" 2>&1 || status=1
timeout --kill-after=15s 5m node --test app_test.mjs > "$RESULT_DIR/node-tests.log" 2>&1 || status=1
node metadata-preflight.mjs . > "$RESULT_DIR/metadata-preflight.json" || status=1
exit "$status"
