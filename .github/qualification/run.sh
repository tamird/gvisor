#!/bin/bash
set -euo pipefail
if test "${HEY_SOURCE_DEADLINE:-}" != 1; then
 exec timeout --signal=TERM --kill-after=120s 118m env HEY_SOURCE_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}"
test "$SOURCE" = ca0536a0eb74b24e306098f6bfb8ee1f1c60742f
test "$BASE" = b29c8320a862a77f4c55894d00c56eb5850c8066
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
control=68c88a589da81e9c77100c8afac0836c40db8a29
support=$(cd "$(dirname "$0")" && pwd)
cleanup() {
 local rc=$?
 trap - EXIT
 trap '' TERM INT
 timeout --kill-after=2s 18s git switch --detach "$SOURCE" || rc=1
 timeout --kill-after=1s 5s git diff --exit-code --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --exit-code --quiet || rc=1
 timeout --kill-after=5s 20s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown || rc=1
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
run() {
 local label="$1" cap="$2" rc=0
 shift 2
 printf '%q ' "$@" >> "$RESULT_DIR/commands.txt"; printf '\n' >> "$RESULT_DIR/commands.txt"
 timeout --kill-after=30s "$cap" "$@" 2>&1 | tee "$RESULT_DIR/$label.log" || rc=$?
 printf '%s\t%s\n' "$label" "$rc" >> "$RESULT_DIR/status.tsv"
 return "$rc"
}
flags='--config=x86_64 -c opt --jobs=3 --local_test_jobs=2 --nostamp --remote_executor= --remote_cache= --bes_backend='
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=$flags")
opts='--nocache_test_results --test_output=all --flaky_test_attempts=1 --test_env=GO_TEST_WRAP_TESTV=1 --test_sharding_strategy=disabled'
run fetch-control 2m git fetch --no-tags origin "$control"
# The negative control changes only tests, retaining BASE's production helper.
test "$(git show -s --format=%P "$control")" = "$BASE"
test "$(git diff --name-only "$BASE" "$control")" = test/benchmarks/tools/hey_test.go
test "$(git rev-parse "$BASE:test/benchmarks/tools/hey.go")" = "$(git rev-parse "$control:test/benchmarks/tools/hey.go")"
run public-test-binding 1m python3 "$support/capture.py" bind "$SOURCE" "$control" "$RESULT_DIR"
git switch --detach "$control"
negative_rc=0
run negative-old-helper 20m make "${make_args[@]}" test TARGETS=//test/benchmarks/tools:tools_test "OPTIONS=$opts --test_filter=TestHeyReport" || negative_rc=$?
test "$negative_rc" != 0
test "$negative_rc" != 124
timeout --kill-after=10s 1m bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc info --config=x86_64 -c opt --remote_executor= --remote_cache= --bes_backend= bazel-testlogs > "$RESULT_DIR/testlogs-path.txt"
logs=$(cat "$RESULT_DIR/testlogs-path.txt")
run negative-proof 1m python3 "$support/capture.py" negative "$logs" "$RESULT_DIR"
git switch --detach "$SOURCE"
status=0
run tools-tests 20m make "${make_args[@]}" test TARGETS=//test/benchmarks/tools:tools_test "OPTIONS=$opts" || status=1
run positive-proof 1m python3 "$support/capture.py" positive "$logs" "$RESULT_DIR" || status=1
run hey-owner-build 25m make "${make_args[@]}" build 'TARGETS=//test/benchmarks/network:nginx_test //test/benchmarks/network:httpd_test //test/benchmarks/network:node_test //test/benchmarks/network:ruby_test' || status=1
run lint 25m make "${make_args[@]}" lint || status=1
run lint-cc 20m make "${make_args[@]}" lint-cc || status=1
exit "$status"
