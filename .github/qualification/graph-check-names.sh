set -euo pipefail
out=$RUNNER_TEMP/graph-bench
mkdir -p "$out"
cp "$GITHUB_WORKSPACE/.github/qualification/graph-check-names.py" "$RUNNER_TEMP/graph-check-names.py"
if python3 "$RUNNER_TEMP/graph-check-names.py" read "$out"; then exit 0; fi
git checkout -q --detach "$PROFILE_CANDIDATE"
plat=$RUNNER_TEMP/overlay-gnu
mkdir -p "$plat"
printf 'module(name="poll_gnu_platform")\n' > "$plat/MODULE.bazel"
printf 'platform(name="gnu",constraint_values=["@platforms//cpu:x86_64","@platforms//os:linux"],exec_properties={"EstimatedMemory":"4GB","Arch":"amd64","OSFamily":"linux","workload-isolation-type":"firecracker","container-image":"docker://%s"},visibility=["//visibility:public"])\n' "$GNU" > "$plat/BUILD"
umask 077
{
  printf '%s\n' 'build --remote_executor=grpcs://remote.buildbuddy.io' 'build --remote_cache=grpcs://remote.buildbuddy.io' 'build --bes_backend=grpcs://remote.buildbuddy.io' 'build --bes_results_url=https://app.buildbuddy.io/invocation/' 'build --spawn_strategy=remote' 'build --noremote_local_fallback' "build --inject_repository=poll_gnu_platform=$plat" 'build --host_platform=@poll_gnu_platform//:gnu' 'build --extra_execution_platforms=@poll_gnu_platform//:gnu' 'build --platforms=//tools/bazeldefs:linux_amd64'
  printf 'build --remote_header=x-buildbuddy-api-key=%s\n' "$BUILDBUDDY_API_KEY"
  printf 'build --bes_header=x-buildbuddy-api-key=%s\n' "$BUILDBUDDY_API_KEY"
} > "$RUNNER_TEMP/overlay.bazelrc"
if [[ -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]]; then sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0; fi
docker pull --platform=linux/amd64 "$GNU" > /dev/null
docker run -d --name graph-names --entrypoint /bin/bash --user root --cap-add SYS_ADMIN --security-opt seccomp=unconfined --security-opt apparmor=unconfined --security-opt systempaths=unconfined -v "$GITHUB_WORKSPACE:$GITHUB_WORKSPACE" -v "$RUNNER_TEMP:$RUNNER_TEMP" -w "$GITHUB_WORKSPACE" "$GNU" -c 'sleep infinity' > /dev/null
d=(docker exec -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0="$GITHUB_WORKSPACE" graph-names)
rc=0
"${d[@]}" bazel --bazelrc="$RUNNER_TEMP/overlay.bazelrc" test -c opt --config=x86_64 --jobs=32 --strategy=GoStdlib=linux-sandbox --strategy=GoStdlibList=linux-sandbox --strategy=GoStaticAnalysis=linux-sandbox --strategy=GoStandardLibraryAnalysis=linux-sandbox --modify_execution_info=^GoStdlib=+no-remote-cache --strategy=TestRunner=remote --test_arg=-test.v '--test_filter=^Test(MapLoadHookReceivers|GraphDecodeMapAndLoadValueDirectStructWithHooks)$' --nocache_test_results '--remote_download_regex=.*test[.]log$' --test_output=errors --remote_download_outputs=toplevel //pkg/state:state_test //pkg/state/tests:tests_test > "$RUNNER_TEMP/named.log" 2>&1 || rc=$?
printf '%s\n' "$rc" > "$out/named-exit"
grep -oE 'https://app.buildbuddy.io/invocation/[a-f0-9-]+' "$RUNNER_TEMP/named.log" | sort -u > "$out/named-invocation"
for target in pkg/state/state_test pkg/state/tests/tests_test; do
  "${d[@]}" cat "bazel-testlogs/$target/test.log" | grep -E '^[[:space:]]*(--- (PASS|FAIL|SKIP):|=== RUN)[[:space:]]+Test(MapLoadHookReceivers|GraphDecodeMapAndLoadValueDirectStructWithHooks)' > "$out/$(basename "$target")-names.txt"
done
test "$rc" = 0
python3 "$RUNNER_TEMP/graph-check-names.py" verify "$out"
