#!/usr/bin/env bash
set -euo pipefail
id > "$RESULT_DIR/environment.txt"
uname -srm >> "$RESULT_DIR/environment.txt"
cat /etc/os-release >> "$RESULT_DIR/environment.txt"
bazel --version >> "$RESULT_DIR/environment.txt"
test "$(id -u)" -ne 0
test "$(sudo -n id -u)" = 0
set +e
bazel aquery --keep_going --output=jsonproto \
    --build_event_json_file="$RESULT_DIR/preflight-events.json" \
    'set(//:repro //:fail)' \
    > "$RESULT_DIR/actions.json" 2> "$RESULT_DIR/preflight.log"
status=$?
set -e
printf '%s\n' "$status" > "$RESULT_DIR/preflight-exit.txt"
test "$status" = 0

for mode in repro fail; do
    set +e
    bazel test --keep_going --cache_test_results=no --test_output=all \
        --local_test_jobs=1 --test_timeout=30 \
        --build_event_json_file="$RESULT_DIR/$mode.bep.json" \
        "//:$mode" > "$RESULT_DIR/$mode.log" 2>&1
    status=$?
    set -e
    printf '%s\n' "$status" > "$RESULT_DIR/$mode.exit"
    printf 'DIRECT_BAZEL_EXIT target=%s status=%s\n' "$mode" "$status"
    cat "$RESULT_DIR/$mode.log"
    logs="bazel-testlogs/$mode"
    if [[ -d "$logs" ]]; then
        sudo -n chown -R "$(id -u):$(id -g)" "$logs"
        chmod -R u+rwX "$logs"
        for file in test.log test.xml; do
            if [[ -f "$logs/$file" ]]; then
                cp "$logs/$file" "$RESULT_DIR/$mode.$file"
            fi
        done
    fi
done
test "$(cat "$RESULT_DIR/fail.exit")" != 0
