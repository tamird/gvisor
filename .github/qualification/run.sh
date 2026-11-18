#!/bin/bash
set -euo pipefail
test "$SOURCE" = 1298a7c6dbc820209ef66bf7a86b8cbb097012d1
test "$(git rev-parse HEAD)" = "$SOURCE"
case "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" in
 amd64:x86_64:x86_64|arm64:aarch64:aarch64) ;;
 *) exit 2 ;;
esac
support=$(cd "$(dirname "$0")" && pwd)
flags=("--config=$QUAL_CONFIG" --jobs=3 --local_test_jobs=2 --nostamp --remote_executor= --remote_cache= --bes_backend=)
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}")
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
trap 'timeout --kill-after=30s 1m "${bazel_cmd[@]}" shutdown || true' EXIT
status=0
run() {
 local label="$1" cap="$2" rc=0
 shift 2
 timeout --kill-after=30s "$cap" "$@" 2>&1 | tee "$RESULT_DIR/$label.log" || rc=$?
 printf '%s\t%s\t%s\n' "$SOURCE" "$label" "$rc" >> "$RESULT_DIR/commands.tsv"
 if test "$rc" != 0; then status=1; fi
 return "$rc"
}
# Only owners whose XML was not captured after the old redundant log assertion.
# Original failed receipts and the already captured TCP/fixture/race XML remain.
opts='--nocache_test_results --test_output=errors --test_sharding_strategy=disabled --flaky_test_attempts=1 --test_env=GO_TEST_WRAP_TESTV=1 --test_env=GOMAXPROCS=2'
run owners 45m make "${make_args[@]}" test 'TARGETS=//pkg/tcpip/transport/tcp/test/e2e:tcp_test //pkg/tcpip/transport/tcp:tcp_nogo' "OPTIONS=$opts --keep_going" || true
logs=$(timeout --kill-after=30s 2m "${bazel_cmd[@]}" info "--config=$QUAL_CONFIG" bazel-testlogs)
python3 "$support/tcp-queue-functional-capture.py" "$logs" "$RESULT_DIR/missing" "$support/tcp-queue-functional-manifest.json" missing || status=1
run lint 25m make "${make_args[@]}" lint || true
if test "$QUAL_ARCH" = amd64; then
 run lint-cc 20m make "${make_args[@]}" lint-cc || true
else
 printf '%s\n' 'ARM64 C++ lint not repeated: this Go-only proposal leaves all C++ files unchanged from the independently verified io_uring baseline failure. No ARM64 C++ pass is claimed.' > "$RESULT_DIR/arm64-cpp-scope.txt"
fi
exit "$status"
