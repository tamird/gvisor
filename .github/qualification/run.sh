#!/bin/bash
set -euo pipefail
test "$SOURCE" = 1298a7c6dbc820209ef66bf7a86b8cbb097012d1
test "$(git rev-parse HEAD)" = "$SOURCE"
git diff --quiet
git diff --cached --quiet
test "$(uname -s)" = Linux
case "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" in
  amd64:x86_64:x86_64|arm64:aarch64:aarch64) ;;
  *) exit 2 ;;
esac
plan_dir="$(dirname "${BASH_SOURCE[0]}")"
mkdir -p "$RESULT_DIR"
file=pkg/tcpip/transport/tcp/endpoint_test.go
test "$(git rev-parse "HEAD:$file")" = 29d79fef4a48d1c881ca0d4d8f89132c23e362cf
cp "$file" "$RESULT_DIR/endpoint_test.original.go"
cp "$plan_dir/tcp-queue-functional-manifest.json" "$RESULT_DIR/fixture-manifest.json"
uname -a > "$RESULT_DIR/worker.txt"
status=0
overlay=false
flags=("--config=$QUAL_CONFIG" --jobs=3 --local_test_jobs=2 --remote_executor= --remote_cache= --bes_backend=)
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}")
cleanup() {
  local rc=$?
  if "$overlay"; then cp "$RESULT_DIR/endpoint_test.original.go" "$file"; fi
  if ! cmp -s "$RESULT_DIR/endpoint_test.original.go" "$file"; then rc=1; fi
  if ! git diff --quiet; then rc=1; fi
  printf '%s\ttracked-tree-restored\t%s\n' "$SOURCE" "$rc" >> "$RESULT_DIR/commands.tsv"
  timeout --kill-after=30s 1m "${bazel_cmd[@]}" shutdown || true
  exit "$rc"
}
trap cleanup EXIT
run() {
  local name="$1" rc=0
  shift
  "$@" 2>&1 | tee "$RESULT_DIR/$name.log" || rc=$?
  printf '%s\t%s\t%s\n' "$SOURCE" "$name" "$rc" >> "$RESULT_DIR/commands.tsv"
  if test "$rc" != 0; then status=1; fi
  return "$rc"
}
capture() {
  local phase="$1" logs
  shift
  logs="$(timeout --kill-after=30s 2m "${bazel_cmd[@]}" info "--config=$QUAL_CONFIG" "$@" bazel-testlogs)"
  python3 "$plan_dir/tcp-queue-functional-capture.py" "$logs" "$RESULT_DIR/$phase" "$plan_dir/tcp-queue-functional-manifest.json" "$phase"
}
opts='--nocache_test_results --test_output=errors --test_sharding_strategy=disabled --flaky_test_attempts=1 --runs_per_test=1 --test_env=GO_TEST_WRAP_TESTV=1 --test_env=GOMAXPROCS=2'
owners='//pkg/tcpip/transport/tcp:tcp_test //pkg/tcpip/transport/tcp:tcp_nogo //pkg/tcpip/transport/tcp/test/e2e:tcp_test'
run native timeout --kill-after=30s 45m make "${make_args[@]}" test "TARGETS=$owners" "OPTIONS=$opts --keep_going" || true
capture native || status=1
overlay=true
python3 - "$file" "$plan_dir/tcp-queue-fixture.go.inc" "$RESULT_DIR/overlay.json" "$SOURCE" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

source, fixture, report = map(Path, sys.argv[1:4])
original = source.read_bytes()
extra = fixture.read_bytes()
assert hashlib.sha256(extra).hexdigest() == 'bd91bc8335fa9182d918e6e9de83c2bf9512621c12591ca18827d19f6bcc79ed'
source.write_bytes(original + extra)
report.write_text(json.dumps({'source': sys.argv[4], 'path': str(source),
    'originalSha256': hashlib.sha256(original).hexdigest(),
    'overlaySha256': hashlib.sha256(original + extra).hexdigest(),
    'fixtureSha256': hashlib.sha256(extra).hexdigest(),
    'scope': 'Temporary test-only fixture; no performance qualification is claimed.'}, indent=2) + '\n')
PY
test "$(git diff --name-only)" = "$file"
selected='^(TestProcessorRetriesUnlockedEndpoint|TestProcessorYieldsAfterReadyEndpoints|TestQueueMembershipAcrossProcessors)'
run supplemental timeout --kill-after=30s 30m make "${make_args[@]}" test TARGETS=//pkg/tcpip/transport/tcp:tcp_test "OPTIONS=$opts --test_arg='-test.run=$selected'" || true
capture supplemental || status=1
fairness='^(TestProcessorRetriesUnlockedEndpoint|TestProcessorYieldsAfterReadyEndpoints)'
onecpu_opts="${opts/--test_env=GOMAXPROCS=2/--test_env=GOMAXPROCS=1}"
run onecpu timeout --kill-after=30s 30m make "${make_args[@]}" test TARGETS=//pkg/tcpip/transport/tcp:tcp_test "OPTIONS=$onecpu_opts --test_arg='-test.run=$fairness'" || true
capture onecpu || status=1
if test "$QUAL_ARCH" = amd64; then
  run race timeout --kill-after=30s 45m make "${make_args[@]}" test TARGETS=//pkg/tcpip/transport/tcp:tcp_test "OPTIONS=$opts --config=race" || true
  capture race --config=race || status=1
fi
printf '%s\n' 'Functional integration only. Full production performance qualification and required broad benchmark sampling remain open. Historical sixteen-pair nonsignificant HTTP results do not close that gate. Lint and C++ lint are outside this bounded functional plan.' > "$RESULT_DIR/validation-scope.txt"
exit "$status"
