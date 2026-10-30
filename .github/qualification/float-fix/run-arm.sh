#!/bin/bash
set -eu -o pipefail
product=0e2d23ed2e6d60a724e0da199bed111050a094c5
test "$(git rev-parse HEAD~2)" = "$product"
cp -R .github/qualification/float-fix "$RUNNER_TEMP/fixture"
git checkout --quiet --detach "$product"
git rev-parse HEAD:pkg/state > "$RUNNER_TEMP/state-tree"
mkdir -p "$RUNNER_TEMP"/{results,focused_platform}
platform=$RUNNER_TEMP/focused_platform
printf '%s\n' 'module(name="focused_platform")' > "$platform/MODULE.bazel"
for arch in amd64 arm64; do
  name=ubuntu
  constraint=x86_64
  if [[ $arch == arm64 ]]; then name=ubuntu_arm64; constraint=aarch64; fi
  cat >> "$platform/BUILD" <<PLATFORM
platform(
    name = "$name",
    constraint_values = ["@platforms//cpu:$constraint", "@platforms//os:linux"],
    exec_properties = {
        "Arch": "$arch",
        "OSFamily": "linux",
        "workload-isolation-type": "oci",
        "container-image": "docker://mirror.gcr.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
    },
    visibility = ["//visibility:public"],
)
PLATFORM
done
export USE_BAZEL_VERSION
USE_BAZEL_VERSION=$(cat images/default/bazelversion)
rc=$RUNNER_TEMP/focused.bazelrc
umask 077
trap 'rm -f "$rc"' EXIT
{
  printf '%s\n' 'build --remote_executor=grpcs://remote.buildbuddy.io' \
    'build --remote_cache=grpcs://remote.buildbuddy.io' \
    'build --bes_backend=grpcs://remote.buildbuddy.io' \
    'build --bes_results_url=https://app.buildbuddy.io/invocation/' \
    'build --spawn_strategy=remote' 'build --noremote_local_fallback' \
    'build --remote_accept_cached' \
    "build --inject_repository=focused_platform=$platform" \
    'build --host_platform=@focused_platform//:ubuntu' \
    'build --extra_execution_platforms=@focused_platform//:ubuntu,@focused_platform//:ubuntu_arm64'
  printf 'build --remote_header=x-buildbuddy-api-key=%s\n' "$BUILDBUDDY_API_KEY"
  printf 'build --bes_header=x-buildbuddy-api-key=%s\n' "$BUILDBUDDY_API_KEY"
} > "$rc"
unset BUILDBUDDY_API_KEY
curl -fsSL --retry 2 --max-time 120 https://github.com/buildbuddy-io/bazel/releases/download/5.0.488/bazel-5.0.488-linux-x86_64 -o "$RUNNER_TEMP/bb"
echo "f0d85b1514f784dc25705dd3bbeeaa1e603b5449f721c26add0245198f03e7cb  $RUNNER_TEMP/bb" | sha256sum -c -
chmod +x "$RUNNER_TEMP/bb"
common=(--remote_download_outputs=toplevel '--remote_download_regex=.*[/]test[.](log|xml)$'
  --nocache_test_results --test_sharding_strategy=disabled --keep_going --test_output=errors)
probe=pkg/state/graph_decode_callbacks_test.go
sed -i '/^[[:space:]]*"fmt"$/a\	"math"' "$probe"
cat "$RUNNER_TEMP/fixture/observation.go" >> "$probe"
test "$(git diff --name-only -- pkg/state)" = "$probe"
rc_runtime=0
bazelisk --bazelrc="$rc" test --config=aarch64 "${common[@]}" --test_arg=-test.v \
  '--modify_execution_info=^TestRunner$=+no-remote-cache' \
  --execution_log_compact_file="$RUNNER_TEMP/runtime.binpb" \
  --build_event_json_file="$RUNNER_TEMP/fix.jsonl" \
  //pkg/state:state_test //pkg/state/tests:tests_test 2>&1 | tee "$RUNNER_TEMP/runtime.log" || rc_runtime=${PIPESTATUS[0]}
echo "$rc_runtime" > "$RUNNER_TEMP/runtime.exit"
python3 -B "$RUNNER_TEMP/fixture/verify-fix.py" fix "$rc_runtime" "$product" || :
git restore -- "$probe"
git diff --exit-code -- pkg/state
rc_nogo=0
bazelisk --bazelrc="$rc" test --config=x86_64 -c opt "${common[@]}" \
  --build_tag_filters= --test_tag_filters= --jobs=4 \
  --strategy=GoStdlib=linux-sandbox --strategy=GoStdlibList=linux-sandbox \
  --strategy=GoStandardLibraryAnalysis=linux-sandbox --strategy=GoStaticAnalysis=linux-sandbox \
  '--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis|StaticAnalysis)$=+no-remote-cache,^TestRunner$=+no-remote-cache' \
  --output_groups=+nogo_findings --execution_log_compact_file="$RUNNER_TEMP/nogo.binpb" \
  --build_event_json_file="$RUNNER_TEMP/nogo.jsonl" \
  //pkg/state:state_nogo //pkg/state/wire:wire_nogo //pkg/state/tests:tests_test_nogo \
  2>&1 | tee "$RUNNER_TEMP/nogo.log" || rc_nogo=${PIPESTATUS[0]}
echo "$rc_nogo" > "$RUNNER_TEMP/nogo.exit"
python3 -B "$RUNNER_TEMP/fixture/verify-arm.py"
