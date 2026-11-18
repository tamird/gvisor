#!/bin/bash
# CPU-native consumer/build qualification only; no hardware execution claim.
set -euo pipefail
if test "${VFIO_CONSUMER_DEADLINE:-}" != 1; then
  exec timeout --signal=TERM --kill-after=120s 180m env VFIO_CONSUMER_DEADLINE=1 bash "${BASH_SOURCE[0]}" "$@"
fi
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}"
test "$SOURCE" = 5d776df85f08eec3cf37a9c2a78f34e1f173b2b2
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$(git show -s --format=%P HEAD)" = "$BASE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
test "$(bazel --version)" = 'bazel 9.3.1'
git diff --quiet
git diff --cached --quiet
support=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
results="$RESULT_DIR/vfio-consumers"
mkdir -m 700 "$results"
status=0
cleanup() {
  local rc=$?
  trap - EXIT TERM INT
  timeout --kill-after=5s 45s sudo chown -R "$(id -u):$(id -g)" "$results" || rc=1
  timeout --kill-after=5s 30s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown || rc=1
  timeout --kill-after=1s 5s git diff --quiet || rc=1
  timeout --kill-after=1s 5s git diff --cached --quiet || rc=1
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
run() {
  local label="$1" cap="$2" rc=0
  shift 2
  printf '%q ' "$@" >> "$results/commands.txt"
  printf '\n' >> "$results/commands.txt"
  rm -f .build_events.json
  timeout --signal=TERM --kill-after=30s "$cap" "$@" 2>&1 | tee "$results/$label.log" || rc=$?
  printf '%s\t%s\n' "$label" "$rc" >> "$results/status.tsv"
  if test -f .build_events.json; then cp .build_events.json "$results/$label.bep"; fi
  if test "$rc" != 0; then status=1; fi
  return "$rc"
}
sudo -n true
uname -a > "$results/worker.txt"
lscpu > "$results/lscpu.txt"
run apt-update 5m sudo apt-get -o Acquire::Retries=2 update
run apt-install 10m sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends build-essential python3 unzip zip
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
  run userns-setup 30s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --nosystem_rc --nohome_rc --host_jvm_args=-Xmx4g' 'BAZEL_OPTIONS=--config=x86_64 --jobs=3 --nostamp --remote_cache= --remote_executor= --bes_backend=')
run consumer-build 50m make "${make_args[@]}" build 'TARGETS=//:release //runsc/gofer/fs //pkg/devutil //pkg/sentry/devices/tpuproxy/vfio //test/runner //test/runner/setup_container //test/syscalls/linux:vfio_pci_device_test //test/syscalls:vfio_pci_device_test_native //test/syscalls:vfio_pci_device_test_runsc_systrap' || true
# groups_test requires CAP_SETGID. Bazel still owns test execution, while its
# standard run_under launcher supplies that capability on this disposable host.
targets='//runsc/gofer/fs:lisafs_test //runsc/gofer/fs:groups_test //test/runner:runner_test'
run consumer-tests 25m make "${make_args[@]}" test "TARGETS=$targets" "OPTIONS=--test_env=GO_TEST_WRAP_TESTV=1 --test_output=errors --nocache_test_results --flaky_test_attempts=1 --run_under='sudo -E'" || true
read -r -a target_args <<< "$targets"
run consumer-accounting 2m sudo -E python3 "$support/collect.py" "$results/test-output" "${target_args[@]}" || true
run source-lint 30m make "${make_args[@]}" lint || true
run cpp-lint 40m make "${make_args[@]}" lint-cc || true
cat > "$results/scope.txt" <<'SCOPE'
Only native AMD64 compile, ordinary gofer/runner consumer tests, and source/C++
lint are covered. The new VFIO PCI test binaries/wrappers are compiled but NOT
executed here. No emulated or physical VFIO device, TPU workload, ARM64 execution,
performance or compatibility-equivalence claim follows from this result.
The separate source-bound KVM VFIO Open qualification is being prepared.
SCOPE
exit "$status"
