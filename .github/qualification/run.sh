#!/bin/bash
set -euo pipefail
if test "${PAWS_DEADLINE_ACTIVE:-}" != 1; then
  exec timeout --signal=TERM --kill-after=180s 180m env PAWS_DEADLINE_ACTIVE=1 bash "${BASH_SOURCE[0]}" "$@"
fi
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}"
test "$SOURCE" = d2c5c08fa3753bcf14ea6015fad547ee8f3d531c
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$(git show -s --format=%P HEAD)" = "$BASE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
test "$(bazel --version)" = 'bazel 9.3.1'
git diff --quiet --exit-code
git diff --cached --quiet --exit-code
support=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
results="$RESULT_DIR/checkpoint-pair"
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
  printf '%s\t%s\t%s\n' "$(git rev-parse HEAD)" "$name" "$rc" >> "$results/status.tsv"
  if test -f .build_events.json; then cp .build_events.json "$results/$name.bep"; fi
  if test "$rc" != 0; then status=1; fi
  return "$rc"
}
# Preserve SOURCE before any paired source switch; do not reset unknown work.
cleanup() {
  local rc=$?
  trap - EXIT
  timeout --signal=TERM --kill-after=5s 30s git switch --detach "$SOURCE" || rc=1
  timeout --signal=TERM --kill-after=5s 30s git diff --quiet --exit-code || rc=1
  timeout --signal=TERM --kill-after=5s 30s git diff --cached --quiet --exit-code || rc=1
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
uname -a > "$results/worker.txt"
lscpu > "$results/lscpu.txt"
# Match images/default/Dockerfile prerequisites. Dual-architecture VDSO export
# invokes /usr/bin/aarch64-linux-gnu-gcc even from an AMD64 qualification host.
run 300 apt-update sudo apt-get -o Acquire::Retries=2 update
run 600 apt-install sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends build-essential crossbuild-essential-arm64 iptables iproute2
for compiler in /usr/bin/gcc /usr/bin/g++ /usr/bin/aarch64-linux-gnu-gcc /usr/bin/aarch64-linux-gnu-g++; do
  test -x "$compiler"
  "$compiler" --version >> "$results/compiler-versions.txt"
done
dpkg-query -W build-essential crossbuild-essential-arm64 gcc-aarch64-linux-gnu g++-aarch64-linux-gnu > "$results/compiler-packages.txt"
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
# The first paired live reproduction is native AMD64 systrap. This does not
# claim native ARM64, KVM, cgroupv2 CI parity, or exact failed-image identity.
unit_targets='//pkg/state:state_test //pkg/sentry/pgalloc:pgalloc_test //runsc/cmd/sentry/forwardcmd:forwardcmd_test'
run 2400 affected-owners make "${make_args[@]}" test 'OPTIONS=--config=x86_64 --test_output=errors --nocache_test_results --flaky_test_attempts=1 --test_sharding_strategy=disabled --test_env=GO_TEST_WRAP_TESTV=1' "TARGETS=$unit_targets" || true
run 180 owners-accounting python3 "$support/collect.py" "$results/affected-owners-output" passed $unit_targets || true
paired_options='--config=x86_64 --test_output=errors --nocache_test_results --flaky_test_attempts=1 --test_sharding_strategy=disabled --runs_per_test=3 --test_filter=TestCheckpointRestoreCrossEngineChained --test_arg=--test_platforms=systrap --test_env=CGROUPV2=false --test_env=GO_TEST_WRAP_TESTV=1'
for side in base candidate; do
  revision="$BASE"
  if test "$side" = candidate; then revision="$SOURCE"; fi
  git diff --quiet --exit-code
  git diff --cached --quiet --exit-code
  git switch --detach "$revision"
  test "$(git rev-parse HEAD)" = "$revision"
  prior_status=$status
  rc=0
  run 3000 "$side-live" make "${make_args[@]}" test "OPTIONS=$paired_options" TARGETS=//runsc/container:container_test || rc=$?
  # BASE failure must be the observed checkpoint dump defect, not any failure.
  if test "$side" = base; then
    if test "$rc" != 0 && grep -q 'error printing state: unreachable' "$results/base-live.log"; then
      status=$prior_status
      printf '%s\n' 'Expected BASE dump failure reproduced; original command failure retained.' > "$results/base-reproduction.txt"
    else
      printf '%s\n' 'BASE did not reproduce the expected dump failure; comparison inconclusive.' > "$results/base-reproduction.txt"
      status=1
    fi
  fi
  run 180 "$side-xml" python3 "$support/collect.py" "$results/$side-live-output" "$side" //runsc/container:container_test || true

done
run 1500 lint make "${make_args[@]}" lint || true
run 900 lint-cc make "${make_args[@]}" lint-cc || true
git diff --quiet --exit-code
git diff --cached --quiet --exit-code
exit "$status"
