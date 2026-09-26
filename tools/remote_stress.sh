#!/bin/bash

# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Run through BuildBuddy Remote Bazel; see remote_stress.md.
set -euo pipefail

if [[ -z "${BUILDBUDDY_CI_RUNNER_ROOT_DIR:-}" || -z "${BUILDBUDDY_ARTIFACTS_DIRECTORY:-}" ]]; then
  echo 'Run this script with bb remote --script; see tools/remote_stress.md.' >&2
  exit 1
fi
targets=()
while (( $# > 0 )); do
  case "$1" in
    --)
      shift
      break
      ;;
    *...*|*\**|*:all|*:all-targets)
      echo "Specify individual test labels, not target patterns: $1" >&2
      exit 1
      ;;
    //*:*) targets+=("$1") ;;
    *)
      echo "Expected an absolute test label, got: $1" >&2
      exit 1
      ;;
  esac
  shift
done
if (( ${#targets[@]} == 0 )); then
  echo 'Specify test labels, followed optionally by -- and test arguments.' >&2
  exit 1
fi
test_args=()
for arg in "$@"; do
  test_args+=("--test_arg=${arg}")
done

runs="${STRESS_RUNS:-100}"
if [[ ! "${runs}" =~ ^[1-9][0-9]*$ ]]; then
  echo "STRESS_RUNS must be a positive integer, got: ${runs}" >&2
  exit 1
fi

isolation="${STRESS_ISOLATION:-oci}"
exec_properties=("--remote_default_exec_properties=workload-isolation-type=${isolation}")
case "${isolation}" in
  oci) ;;
  firecracker) exec_properties+=(--remote_default_exec_properties=dockerUser=root) ;;
  *)
    echo "STRESS_ISOLATION must be oci or firecracker, got: ${isolation}" >&2
    exit 1
    ;;
esac

{
  git rev-parse HEAD
  printf 'runs_per_test=%s\n' "${runs}"
  printf 'isolation=%s\n' "${isolation}"
  printf 'target=%s\n' "${targets[@]}"
  if (( $# > 0 )); then
    printf 'test_arg=%s\n' "$@"
  fi
} > "${BUILDBUDDY_ARTIFACTS_DIRECTORY}/stress.txt"

# The hosted runner injects this RBE config and its authentication. Keep test
# retries and caching disabled so every requested run contributes its result.
exec bazel --host_jvm_args=-Xmx2g --host_jvm_args=-XX:ActiveProcessorCount=2 test \
  --config=buildbuddy_remote_executor \
  --config=x86_64 \
  --remote_default_exec_properties=OSFamily=linux \
  --remote_default_exec_properties=Arch=amd64 \
  --remote_default_exec_properties=container-image=docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02 \
  "${exec_properties[@]}" \
  --spawn_strategy=remote \
  --strategy=TestRunner=remote \
  --noremote_local_fallback \
  --remote_download_outputs=minimal \
  --remote_cache_compression \
  --jobs=32 \
  --loading_phase_threads=2 \
  --runs_per_test="${runs}" \
  --nocache_test_results \
  --flaky_test_attempts=1 \
  --noruns_per_test_detects_flakes \
  --test_timeout=60 \
  --test_output=errors \
  --keep_going \
  "${test_args[@]}" \
  "${targets[@]}"
