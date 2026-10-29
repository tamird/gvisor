#!/bin/bash
set -eu
workspace=$1
tmp=$2
out=$tmp/static-consumer
rc=$tmp/consumer.bazelrc
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$workspace"
cd "$workspace"
test "$(git rev-parse HEAD)" = b9193eed0d34d1ad1aa0a5da5603c82871052e2e
base=(bazel --bazelrc="$rc")
flags=(--config=x86_64 --@io_bazel_rules_go//go/config:pure=false --jobs=36 --remote_timeout=15m --remote_download_outputs=toplevel)
status=0
"${base[@]}" build "${flags[@]}" --build_event_json_file="$out/build.bep" //test/cmd/test_app:test_app //test/benchmarks/base:startup_test >"$out/build.log" 2>&1 || status=$?
echo "$status" > "$out/build.exit"
if [[ $status == 0 ]]; then
  mkdir -p "$out/materialized"
  {
  set +e
  for pair in test/cmd/test_app:test_app test/benchmarks/base:startup_test; do
    label="//${pair}"
    path=$("${base[@]}" cquery --config=x86_64 --@io_bazel_rules_go//go/config:pure=false --output=starlark --starlark:expr='providers(target)["DefaultInfo"].files_to_run.executable.path' "$label" 2>>"$out/build.log")
    er=$("${base[@]}" info execution_root 2>>"$out/build.log")
    cp -L -- "$er/$path" "$out/materialized/${pair#*:}"
  done
  set -e
  }
fi
  status=0
  "${base[@]}" test "${flags[@]}" --nocache_test_results --modify_execution_info='^TestRunner$=+no-remote-cache' --remote_download_regex='.*[/]test[.](log|xml)$' --test_timeout=300 --test_sharding_strategy=disabled --test_filter='^TestRootNotMount$' --test_arg=-test.v --build_event_json_file="$out/test.bep" //runsc/container:container_test >"$out/test.log.native" 2>&1 || status=$?
  echo "$status" > "$out/test.exit"
  tl=$("${base[@]}" info bazel-testlogs 2>>"$out/test.log.native")
  for file in test.log test.xml; do
    [[ ! -f "$tl/runsc/container/container_test/$file" ]] || cp -L -- "$tl/runsc/container/container_test/$file" "$out/$file"
  done
chmod -R a+rX "$out"
