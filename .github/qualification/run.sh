#!/bin/bash
set -euo pipefail
if test "${IPERF_SOURCE_DEADLINE:-}" != 1; then
 exec timeout --signal=TERM --kill-after=120s 118m env IPERF_SOURCE_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}"
test "$SOURCE" = ead57716c09ee8f986ac0b0d6576994ea94407d0
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
control=ca7a3f4d284f50dfcd78a05502b18424b136daba
support=$(cd "$(dirname "$0")" && pwd)
cleanup() {
 local rc=$?
 trap - EXIT TERM INT
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
test "$(git diff --name-only "$BASE" "$control")" = test/benchmarks/tools/iperf_test.go
test "$(git rev-parse "$BASE:test/benchmarks/tools/iperf.go")" = "$(git rev-parse "$control:test/benchmarks/tools/iperf.go")"
run public-test-binding 1m python3 "$support/capture.py" bind "$SOURCE" "$control" "$RESULT_DIR"
git switch --detach "$control"
negative_rc=0
run negative-old-helper 20m make "${make_args[@]}" test TARGETS=//test/benchmarks/tools:tools_test "OPTIONS=$opts --test_filter=TestIperfReport" || negative_rc=$?
test "$negative_rc" != 0
test "$negative_rc" != 124
timeout --kill-after=10s 1m bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc info --config=x86_64 -c opt --remote_executor= --remote_cache= --bes_backend= bazel-testlogs > "$RESULT_DIR/testlogs-path.txt"
logs=$(cat "$RESULT_DIR/testlogs-path.txt")
run negative-proof 1m python3 "$support/capture.py" negative "$logs" "$RESULT_DIR"
git switch --detach "$SOURCE"
status=0
run tools-tests 20m make "${make_args[@]}" test TARGETS=//test/benchmarks/tools:tools_test "OPTIONS=$opts" || status=1
run positive-proof 1m python3 "$support/capture.py" positive "$logs" "$RESULT_DIR" || status=1
run iperf-owner-build 25m make "${make_args[@]}" build TARGETS=//test/benchmarks/network:iperf_test || status=1
run lint 25m make "${make_args[@]}" lint || status=1
run lint-cc 20m make "${make_args[@]}" lint-cc || status=1
exit "$status"
