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

set -uo pipefail
trap 'exit 130' INT
trap 'exit 143' TERM

lanes=(nogo unit smoke smoke-race docker root portforward posture syscalls)

usage() {
  cat <<'USAGE'
Usage: test/rbe/qualify.sh amd64
       test/rbe/qualify.sh LANE [LANE ...]
       test/rbe/qualify.sh --list

Run the implemented Linux AMD64 remote lanes using the configured Bazel RBE
connection. This is partial public CI coverage. Existing failures remain errors.
USAGE
  printf '\nLanes: %s\n' "${lanes[*]}"
}

gaps() {
  cat <<'GAPS'
Unqualified by this profile: KVM and slimvm; native ARM64; cgroup v1, the
host systemd cgroup manager and alternate host kernels; the full save/restore
and coverage matrices; containerd, networking, GPU, and network-plugin lanes.
GAPS
}

if (( $# == 0 )); then
  usage >&2
  exit 2
fi
if [[ $# == 1 && ( $1 == --list || $1 == --help ) ]]; then
  usage
  gaps
  exit 0
fi
if [[ $# == 1 && $1 == amd64 ]]; then
  set -- "${lanes[@]}"
fi
# Validate every requested lane before starting any work.
for lane in "$@"; do
  case "$lane" in
    nogo|unit|smoke|smoke-race|docker|root|portforward|posture|syscalls) ;;
    *) printf 'Unknown lane: %s\n' "$lane" >&2; usage >&2; exit 2 ;;
  esac
done
# The coordinator must be Linux; never start Bazel on the macOS host.
if [[ $(uname -s) != Linux ]]; then
  printf 'Run this command on a Linux remote coordinator.\n' >&2
  exit 2
fi
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

printf 'Selected Linux AMD64 remote lanes: %s\n' "$*"
gaps

run_lane() {
  local lane=$1
  local -a options=() targets=()
  case "$lane" in
    nogo)
      options=(--config=nogo)
      targets=(//...)
      ;;
    unit)
      # test/unit.targets also retains non-test build targets and the existing
      # exclusions. Keep its selection separate from Nogo's positive tag filter.
      options=(--config=unit)
      ;;
    smoke)
      targets=(//:release_smoke_test)
      ;;
    smoke-race)
      options=(--config=race)
      targets=(//:release_smoke_test)
      ;;
    docker)
      options=(--config=docker)
      targets=(//test/docker:owned_tests)
      ;;
    root)
      options=(--config=docker)
      targets=(//test/root:root_test_owned)
      ;;
    portforward)
      options=(--config=docker)
      targets=(//test/root:portforward_test_owned)
      ;;
    posture)
      options=(--config=docker --test_tag_filters=-requires-kvm)
      targets=(//test/root:sandbox_posture_test_owned)
      ;;
    syscalls)
      options=(--target_pattern_file=test/syscalls.targets --cxxopt=-Werror
        '--test_tag_filters=-nogo,-allsave,-runsc_kvm,-runsc_slimvm')
      ;;
  esac
  bazel test --config=rbe --config=x86_64 \
    --strip=never --incompatible_sandbox_hermetic_tmp=false \
    --keep_going --test_output=errors "${options[@]}" "${targets[@]}"
}

status=0
for lane in "$@"; do
  printf '\nRunning lane: %s\n' "$lane"
  lane_status=0
  run_lane "$lane" || lane_status=$?
  printf 'Lane %s exited %d\n' "$lane" "$lane_status"
  if (( lane_status != 0 )); then
    status=1
  fi
done
exit "$status"
