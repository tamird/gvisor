#!/bin/bash
set -euo pipefail
if test "${PAWS_DEADLINE_ACTIVE:-}" != 1; then
  exec timeout --signal=TERM --kill-after=60s 180m env PAWS_DEADLINE_ACTIVE=1 bash "${BASH_SOURCE[0]}" "$@"
fi
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}"
test "$SOURCE" = 983dc3a36b4c481da9911e05b97d7a02b18b8e71
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$(git show -s --format=%P HEAD)" = "$BASE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = arm64:aarch64:aarch64
test "$(bazel --version)" = 'bazel 9.3.1'
git diff --quiet --exit-code
git diff --cached --quiet --exit-code
support=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
results="$RESULT_DIR/paws"
mkdir -m 700 "$results"
status=0
export GO_TEST_WRAP_TESTV=1
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --nosystem_rc --nohome_rc --host_jvm_args=-Xmx4g' 'BAZEL_OPTIONS=--jobs=3 --local_test_jobs=2 --nostamp --remote_cache= --remote_executor= --bes_backend= --bes_results_url=')
run() {
  local seconds="$1" name="$2" rc=0
  shift 2
  printf '%q ' "$@" >> "$results/commands.txt"
  printf '\n' >> "$results/commands.txt"
  # This task owns this disposable runner. Remove only its prior event stream
  # so a build that emits no BEP cannot inherit the previous test's evidence.
  rm -f .build_events.json
  timeout --signal=TERM --kill-after=60s "${seconds}s" "$@" 2>&1 | tee "$results/$name.log" || rc=$?
  printf '%s\t%s\t%s\n' "$SOURCE" "$name" "$rc" >> "$results/status.tsv"
  if test -f .build_events.json; then cp .build_events.json "$results/$name.bep"; fi
  if test "$rc" != 0; then status=1; fi
  return "$rc"
}
options='--config=aarch64 --test_output=errors --nocache_test_results --flaky_test_attempts=1 --test_env=GO_TEST_WRAP_TESTV=1'
test_phase() {
  local seconds="$1" name="$2" extra="$3" targets="$4" rc=0
  run "$seconds" "$name" make "${make_args[@]}" test "OPTIONS=$options $extra" "TARGETS=$targets" || rc=$?
  # Account immediately; subsequent configurations may replace bazel-testlogs.
  local target_args=()
  read -r -a target_args <<< "$targets"
  run 120 "$name-accounting" python3 "$support/collect.py" "$results/$name-output" "${target_args[@]}" || rc=1
  return "$rc"
}
uname -a > "$results/worker.txt"
lscpu > "$results/lscpu.txt"
# Native ARM64 missing-only tests do not run dual-architecture export.
run 300 apt-update sudo apt-get -o Acquire::Retries=2 update
run 600 apt-install sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends build-essential iptables iproute2
uname -m | grep -x aarch64
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
test_phase 2400 tcp-tests '' '//pkg/tcpip/transport/tcp:tcp_test //pkg/tcpip/transport/tcp/test/e2e:tcp_test //pkg/tcpip/transport/tcp/test/e2e:tcp_timestamp_test' || true
test_phase 2100 timestamp-race --config=race //pkg/tcpip/transport/tcp/test/e2e:tcp_timestamp_test || true
if test_phase 1500 paws-linux '' //test/packetimpact/tests:tcp_paws_mechanism_native_test; then
  test_phase 1500 paws-netstack '' //test/packetimpact/tests:tcp_paws_mechanism_netstack_test || true
else
  printf '%s\tpaws-netstack\tskipped-native-failed\n' "$SOURCE" >> "$results/status.tsv"
fi
git diff --quiet --exit-code
git diff --cached --quiet --exit-code
exit "$status"
