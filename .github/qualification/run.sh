#!/bin/bash
set -euo pipefail
if test "${INLINE_CONTROL_DEADLINE_ACTIVE:-}" != 1; then
  exec timeout --signal=TERM --kill-after=90s 60m env INLINE_CONTROL_DEADLINE_ACTIVE=1 bash "${BASH_SOURCE[0]}" "$@"
fi
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${RESULT_DIR:?}"
test "$SOURCE" = d2c5c08fa3753bcf14ea6015fad547ee8f3d531c
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$QUAL_ARCH:$(uname -m)" = amd64:x86_64
test "$(git rev-parse HEAD)" = "$SOURCE"
git diff --quiet
git diff --cached --quiet
support=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
results="$RESULT_DIR/checkpoint-inline-control"
mkdir -m 700 "$results"
restoration_ready=0
cleanup() {
  local rc=$?
  trap - EXIT
  trap '' TERM INT
  if test "$restoration_ready" = 1; then
    timeout --signal=KILL 5s cp "$results/original-test.go" runsc/cmd/sentry/forwardcmd/statefile_test.go || rc=1
    timeout --signal=KILL 5s cp "$results/original-BUILD" runsc/cmd/sentry/forwardcmd/BUILD || rc=1
  fi
  timeout --signal=TERM --kill-after=5s 20s git switch --detach "$SOURCE" || rc=1
  timeout --signal=TERM --kill-after=1s 5s git diff --quiet || rc=1
  timeout --signal=TERM --kill-after=1s 5s git diff --cached --quiet || rc=1
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
# Only tests change. No production source, runtime flags, or decoder is patched.
git switch --detach "$BASE"
test "$(git rev-parse HEAD)" = "$BASE"
cp runsc/cmd/sentry/forwardcmd/statefile_test.go "$results/original-test.go"
cp runsc/cmd/sentry/forwardcmd/BUILD "$results/original-BUILD"
restoration_ready=1
cp "$support/statefile_test.go" runsc/cmd/sentry/forwardcmd/statefile_test.go
cp "$support/BUILD" runsc/cmd/sentry/forwardcmd/BUILD
git diff --binary > "$results/test-only.patch"
python3 "$support/verify-inputs.py" "$results"
rm -f .build_events.json
rc=0
timeout --signal=TERM --kill-after=60s 2700s make DOCKER_BUILD=false 'BAZEL=bazel --nosystem_rc --nohome_rc --host_jvm_args=-Xmx4g' 'BAZEL_OPTIONS=--jobs=3 --local_test_jobs=1 --nostamp --remote_cache= --remote_executor= --bes_backend= --bes_results_url=' test 'OPTIONS=--config=x86_64 --test_output=errors --nocache_test_results --flaky_test_attempts=1 --test_sharding_strategy=disabled --test_filter=TestStatefileDumpMemoryFiles --test_env=GO_TEST_WRAP_TESTV=1' TARGETS=//runsc/cmd/sentry/forwardcmd:forwardcmd_test > "$results/baseline.log" 2>&1 || rc=$?
printf '%s\n' "$rc" > "$results/baseline-command-exit.txt"
test -f .build_events.json
cp .build_events.json "$results/baseline.bep"
python3 "$support/collect.py" "$results"
