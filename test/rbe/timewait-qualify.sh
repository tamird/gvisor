#!/bin/bash
set -eu -o pipefail
arm=$1
case "$arm" in
  linux) source=5399c033f03b6c993a794c2df6d90dd3d4b3187c; fixture=f430430e866e59f394dc4fece78a7cb73b3275b5; expected=8.8.1; label=//test/packetimpact/tests:tcp_timewait_reset_native_test; filter='^TestTimeWaitSynAck$';;
  proposed) source=9a707824b19d69c0d389cb6c22889963fc6895be; fixture=$source; expected=8.3.1; label=//pkg/tcpip/transport/tcp/test/e2e:forwarder_test; filter='^TestForwarder(ReopenAfterTimeWait|TimeWaitSynAck)$';;
  fixed) source=2bc369a8afc2c629597b38a58da55374e8f61cc5; fixture=$source; expected=8.3.1; label=//pkg/tcpip/transport/tcp/test/e2e:forwarder_test; filter='^TestForwarder(ReopenAfterTimeWait|TimeWaitSynAck)$';;
esac
git -c safe.directory="$PWD" switch -q --detach "$source"
# actions/checkout was performed by the runner's uid; this root container
# also invokes git from Bazel's workspace status hook.
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$PWD"
test "$(git rev-parse HEAD)" = "$source"
case "$arm" in
  linux)
    test "$(git rev-parse HEAD^)" = "$fixture"
    test "$(git rev-parse "$fixture^")" = 8f9e31eddd9c72048f13bf807f333ec6e33a64f4
    git diff --quiet "$fixture" HEAD -- pkg test/packetimpact/tests test/packetimpact/testbench
    test "$(git diff --name-only "$fixture^" "$fixture")" = test/packetimpact/tests/tcp_timewait_reset_test.go
    ;;
  proposed) test "$(git rev-parse HEAD^)" = e503a89e87b363812834ccaa96eb503446490faa
    test "$(git diff --name-only HEAD^ HEAD)" = pkg/tcpip/transport/tcp/test/e2e/forwarder_test.go;;
  fixed) test "$(git rev-parse HEAD^)" = 9a707824b19d69c0d389cb6c22889963fc6895be
    test "$(git diff --name-only HEAD^ HEAD)" = pkg/tcpip/transport/tcp/rcv.go;;
esac
export USE_BAZEL_VERSION
USE_BAZEL_VERSION=$(cat images/default/bazelversion)
test "$USE_BAZEL_VERSION" = "$expected"
rc=$RUNNER_TEMP/tcp.bazelrc
work=$RUNNER_TEMP/measure-private/$arm
pub=$RUNNER_TEMP/measure-public
mkdir -p "$work" "$pub/$arm"
"$RUNNER_TEMP/bazel-$USE_BAZEL_VERSION" --version > "$work/version"
grep -Fx "bazel $expected" "$work/version"
if [[ $arm == linux ]]; then
    "$RUNNER_TEMP/bazel-$USE_BAZEL_VERSION" --batch --bazelrc="$rc" test --config=x86_64 --strategy=TestRunner=local --jobs=3 --test_env=FENCE_HOST_ID --test_env=FENCE_HOST_PROBE --test_env=XTABLES_LOCKFILE --nocache_test_results --test_output=all //test/rbe/fence_host_probe:check > "$work/preflight.log" 2>&1 || { tail -40 "$work/preflight.log"; exit 2; }
    grep -F FENCE_HOST_OK "$work/preflight.log" > "$pub/$arm/preflight"
fi
# The unchanged 8.3.1 source writes goimports.sh without a shebang.
# Its native linux-sandbox launcher calls execvp (including its shell fallback);
# the remote executor passes ENOEXEC straight through.
flags=(--config=x86_64 --strategy=GoImports=linux-sandbox --jobs=3 --strategy=CppCompile=linux-sandbox,local --strategy=CppLink=linux-sandbox,local --strategy=TestRunner=local --local_test_jobs=1 --test_env=XTABLES_LOCKFILE --test_env=GO_TEST_WRAP_TESTV=1 --test_env=GVISOR_TEST_CLOCK_SOURCE=calibrated --define=gvisor_test_clock_source=calibrated --nocache_test_results --test_sharding_strategy=disabled --test_timeout=350 --test_output=errors --remote_download_outputs=toplevel '--remote_download_regex=.*[/]test[.](log|xml)$' "--test_filter=$filter" --build_event_json_file_path_conversion=false "--build_event_json_file=$work/raw.bep")
if [[ $arm != linux ]]; then flags+=(--test_arg=-test.v); fi
git cat-file commit HEAD > "$work/commit_file"
git status --porcelain --untracked-files=no > "$work/source_before"; test ! -s "$work/source_before"
printf '%s\n' test "${flags[@]}" "$label" > "$work/executed_arguments"
code=0
"$RUNNER_TEMP/bazel-$USE_BAZEL_VERSION" --batch --bazelrc="$rc" test "${flags[@]}" "$label" > "$work/bazel.log" 2>&1 || code=$?
date -u '+%Y-%m-%dT%H:%M:%S.%3N+00:00' > "$work/finished"
printf '%s\n' "$code" > "$work/native_exit"
git status --porcelain --untracked-files=no > "$work/source_after"
git rev-parse HEAD > "$work/final_head"
tail -22 "$work/bazel.log"
python3 -B "$RUNNER_TEMP/checker/measure.py" "$work" "$arm" "$source" "$fixture" "$label" "$pub"
