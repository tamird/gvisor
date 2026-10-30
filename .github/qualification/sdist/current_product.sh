#!/bin/bash
set -euo pipefail
out="$RUNNER_TEMP/publication"
test "$(git rev-parse HEAD)" = "$CURRENT_PRODUCT"
export USE_BAZEL_VERSION
USE_BAZEL_VERSION=$(cat images/default/bazelversion)
rc="$RUNNER_TEMP/current-release.bazelrc"
umask 077
printf 'build --remote_header=x-buildbuddy-api-key=%s\n' "$BUILDBUDDY_API_KEY" > "$rc"
unset BUILDBUDDY_API_KEY
for lane in gnu scratch; do
  data="$RUNNER_TEMP/release-$lane"
  mkdir -p "$data"
  flags=(--spawn_strategy=local -c opt --//sandboxexec/sandbox/python:release_version=2026.10.29.2)
  if [[ $lane == scratch ]]; then
    flags+=(--config=static-python --remote_executor=grpcs://remote.buildbuddy.io --remote_cache=grpcs://remote.buildbuddy.io
      --remote_default_exec_properties=OSFamily=linux --remote_default_exec_properties=Arch=amd64
      --remote_default_exec_properties=workload-isolation-type=oci
      --remote_default_exec_properties=container-image=docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef
      --strategy=PythonDistribution=remote --remote_download_outputs=toplevel)
  fi
  status=0
  bazelisk --bazelrc="$rc" build --lockfile_mode=update --modify_execution_info='^PythonDistribution$=+no-remote-cache' --build_event_json_file="$data/events" --execution_log_compact_file="$data/execution.binpb" "${flags[@]}" //sandboxexec/sandbox/python:dist >"$data/private.log" 2>&1 || status=$?
  echo "$status" >"$out/current-$lane.exit"
  if [[ -s "$data/execution.binpb" ]]; then
    "$RUNNER_TEMP/bb" print --compact_execution_log="$data/execution.binpb" --raw=true --sort=false >"$data/spawns"
  fi
  python3 -B "$RUNNER_TEMP/sdist-fixture/current_result.py" "$lane" "$data" "$out" || :
done
test "$(cat "$out/current-gnu.exit")" = 0 && test "$(cat "$out/current-scratch.exit")" = 0
