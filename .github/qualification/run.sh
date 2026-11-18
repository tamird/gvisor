#!/bin/bash
# Source-bound VFIO Open in the existing isolated KVM guest harness.
set -euo pipefail
if test "${VFIO_KVM_DEADLINE:-}" != 1; then
 exec timeout --signal=TERM --kill-after=180s 180m env VFIO_KVM_DEADLINE=1 bash "${BASH_SOURCE[0]}" "$@"
fi
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}"
test "$SOURCE" = 1b37f9cfca65287f8c153314e392d4fbf7c4094b
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$(git rev-parse HEAD^)" = "$BASE"
git diff --quiet
git diff --cached --quiet
test "$(bazel --version)" = 'bazel 9.3.1'
carrier=31cdf84479594df7215c4dcf20257a06d921f0f0
support=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
w="$RUNNER_TEMP/vfio-kvm"
results="$RESULT_DIR/vfio-kvm"
mkdir -m 700 "$w" "$results"
cleanup() {
 local rc=$?
 trap - EXIT TERM INT
 # Remove only this plan's staged harness input, then restore the precise
 # historical payload before returning to the untouched current product.
 if test -f "$w/carrier-active"; then
  timeout --kill-after=2s 8s rm -f test/rbe/tcg/vfio_suite.tar || rc=1
  timeout --kill-after=2s 8s git restore --source="$carrier" --worktree test/rbe/tcg/vfio_permanent.sh || rc=1
 fi
 timeout --kill-after=5s 20s git switch --detach "$SOURCE" || rc=1
 timeout --kill-after=1s 5s git diff --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --quiet || rc=1
 timeout --kill-after=5s 45s sudo chown -R "$(id -u):$(id -g)" "$results" || rc=1
 timeout --kill-after=5s 30s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown || rc=1
 printf '%s\n' "$rc" > "$results/final-status.txt"
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
stage() {
 local label="$1" cap="$2" rc=0
 shift 2
 printf '%q ' "$@" >> "$results/commands.txt"; printf '\n' >> "$results/commands.txt"
 timeout --kill-after=30s "$cap" "$@" 2>&1 | tee "$results/$label.log" || rc=$?
 printf '%s\t%s\n' "$label" "$rc" >> "$results/status.tsv"
 return "$rc"
}
sudo -n true
stage kvm-device 20s sudo bash -c 'test -c /dev/kvm && test -r /dev/kvm && test -w /dev/kvm'
uname -a > "$results/worker.txt"
lscpu > "$results/lscpu.txt"
stage apt-update 5m sudo apt-get -o Acquire::Retries=2 update
stage apt-install 15m sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends build-essential python3 unzip zip gcc-aarch64-linux-gnu g++-aarch64-linux-gnu
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
 stage userns-setup 30s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
flags=(--config=x86_64 --jobs=3 --nostamp --remote_cache= --remote_executor= --bes_backend=)
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}")
stage product-build 55m make "${make_args[@]}" build 'TARGETS=//:release //test/runner:runner //test/runner/setup_container //test/syscalls/linux:vfio_pci_device_test //test/syscalls:vfio_pci_device_test_native //test/syscalls:vfio_pci_device_test_runsc_systrap'
stage wrapper-query 2m "${bazel_cmd[@]}" query --output=build 'set(//test/syscalls:vfio_pci_device_test_native //test/syscalls:vfio_pci_device_test_runsc_systrap)'
test "$(grep -Fc -- '--add-host-vfio' "$results/wrapper-query.log")" = 2
test "$(grep -Fc -- 'exclusive-if-local' "$results/wrapper-query.log")" = 2
stage freeze-product 3m python3 "$support/freeze.py" "$w/suite" "$results"
# Fetch only the already identified public historical harness carrier. It uses
# its own committed MODULE pins; no former remote executor/auth is inherited.
stage carrier-fetch 3m git fetch --no-tags --depth=1 https://github.com/tamird/gvisor.git "$carrier"
stage carrier-switch 30s git switch --detach "$carrier"
touch "$w/carrier-active"
test "$(git rev-parse HEAD)" = "$carrier"
cp .bazelversion "$results/carrier-bazelversion.txt"
cp .bazelrc "$results/carrier-bazelrc.txt"
cp MODULE.bazel "$results/carrier-MODULE.bazel"
# This qualification-only payload adds Linux-first gating and retains each XML.
cp "$support/vfio_permanent.sh" test/rbe/tcg/vfio_permanent.sh
cp "$w/suite.tar" test/rbe/tcg/vfio_suite.tar
sha256sum test/rbe/tcg/vfio_permanent.sh test/rbe/tcg/vfio_suite.tar > "$results/harness-input-hashes.txt"
under=$(printf '%q ' sudo -E bash "$support/kvm-under.sh" "$(id -u)" "$(id -g)")
rc=0
stage kvm-vfio-open 80m "${bazel_cmd[@]}" test "${flags[@]}" --strategy=TestRunner=local --nocache_test_results --flaky_test_attempts=1 --nozip_undeclared_test_outputs --test_output=all --test_timeout=900 "--run_under=$under" //test/rbe/tcg:vfio_permanent_test || rc=$?
stage collect-guest 2m sudo -E python3 "$support/collect.py" "$results" || rc=1
exit "$rc"
