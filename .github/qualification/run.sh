#!/bin/bash
set -euo pipefail
if test "${BENCH_INTEGRATION_DEADLINE:-}" != 1; then
 exec timeout --signal=TERM --kill-after=120s 58m env BENCH_INTEGRATION_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}"
test "$SOURCE" = af131d57abcab71b361073a48efd8607959aa5ae
test "$BASE" = 2b27d0c3ad88e71cacf9e5b9602b7b8cbed6c69e
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
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
run source-binding 1m python3 "$support/capture.py" bind "$support/expected.json" "$RESULT_DIR" "$SOURCE"
status=0
run integrated-tools-tests 20m make "${make_args[@]}" test TARGETS=//test/benchmarks/tools:tools_test "OPTIONS=$opts" || status=1
timeout --kill-after=10s 1m bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc info --config=x86_64 -c opt --remote_executor= --remote_cache= --bes_backend= bazel-testlogs > "$RESULT_DIR/testlogs-path.txt"
logs=$(cat "$RESULT_DIR/testlogs-path.txt")
run exact-xml-union 1m python3 "$support/capture.py" xml "$support/expected.json" "$RESULT_DIR" "$logs" || status=1
run four-consumer-builds 25m make "${make_args[@]}" build 'TARGETS=//test/benchmarks/network:nginx_test //test/benchmarks/network:httpd_test //test/benchmarks/network:node_test //test/benchmarks/network:ruby_test' || status=1
exit "$status"
