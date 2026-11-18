#!/bin/bash
# Missing native-owner output validation only; completed builds/lints stay reused.
set -euo pipefail
if test "${VFIO_OWNER_DEADLINE:-}" != 1;then
 exec timeout --signal=TERM --kill-after=120s 70m env VFIO_OWNER_DEADLINE=1 bash "${BASH_SOURCE[0]}" "$@"
fi
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}"
test "$SOURCE" = 1b37f9cfca65287f8c153314e392d4fbf7c4094b
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$(git rev-parse HEAD^)" = "$BASE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
test "$(bazel --version)" = 'bazel 9.3.1'
support=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
results="$RESULT_DIR/vfio-owners"
mkdir -m 700 "$results"
cleanup() {
 local rc=$?
 trap - EXIT TERM INT
 timeout --kill-after=5s 45s sudo chown -hR "$(id -u):$(id -g)" "$results" || rc=1
 timeout --kill-after=5s 30s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown || rc=1
 timeout --kill-after=1s 5s git diff --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --quiet || rc=1
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
git diff --quiet
git diff --cached --quiet
sudo -n true
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns;then timeout --kill-after=5s 30s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0;fi
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' 'BAZEL_OPTIONS=--config=x86_64 --jobs=3 --nostamp --remote_cache= --remote_executor= --bes_backend=')
under=$(printf '%q ' sudo -E bash "$support/root-test-under.sh" "$(id -u)" "$(id -g)")
targets=(//runsc/gofer/fs:lisafs_test //runsc/gofer/fs:groups_test //test/runner:runner_test)
rm -f .build_events.json
rc=0
timeout --kill-after=30s 55m make "${make_args[@]}" test "TARGETS=${targets[*]}" "OPTIONS=--test_env=GO_TEST_WRAP_TESTV=1 --test_output=errors --nocache_test_results --flaky_test_attempts=1 --strategy=TestRunner=local --run_under='$under'" 2>&1 | tee "$results/owners.log" || rc=$?
if test -f .build_events.json;then cp .build_events.json "$results/owners.bep";fi
sudo -E python3 "$support/collect.py" "$results/test-output" "${targets[@]}" || rc=1
python3 "$support/bep.py" "$results/owners.bep" "$results/bep-accounting.json" "${targets[@]}" || rc=1
printf '%s\n' "$rc" > "$results/owner-status.txt"
exit "$rc"
