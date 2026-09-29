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

lanes=(nogo unit smoke smoke-race release-artifacts release-repository docker root portforward posture startup containerd bwrap packetimpact iptables nftables packetdrill language-directfs language-goferfs kubernetes podman website go-export workflows syscalls syscalls-save syscalls-resume)

usage() {
  cat <<'USAGE'
Usage: test/rbe/qualify.sh amd64
       test/rbe/qualify.sh [--arch=amd64|arm64] LANE [LANE ...]
       test/rbe/qualify.sh --list

Run Linux remote lanes using the configured Bazel RBE connection. The default
target architecture is AMD64. Tests use matching execution workers; release
artifacts cross-build on AMD64 workers. This is partial public CI coverage;
selecting an architecture does not guarantee worker support. Existing failures
remain errors.
USAGE
  printf '\nLanes: %s\n' "${lanes[*]}"
}

gaps() {
  cat <<'GAPS'
Unqualified by this profile: KVM and slimvm; the full ARM64 matrix; cgroup v1, the
host systemd cgroup manager and alternate host kernels; the full save/restore
and coverage matrices; GPU and network-plugin lanes; staged-binary consistency.
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
arch=amd64
if [[ $1 == --arch=* ]]; then
  arch=${1#--arch=}
  shift
fi
case "$arch" in
  amd64) architecture_config=x86_64 ;;
  arm64) architecture_config=aarch64 ;;
  *) printf 'Unknown architecture: %s\n' "$arch" >&2; exit 2 ;;
esac
if (( $# == 0 )); then
  usage >&2
  exit 2
fi
# Validate every requested lane before starting any work.
for lane in "$@"; do
  case "$lane" in
    nogo|unit|smoke|smoke-race|release-artifacts|release-repository|docker|root|portforward|posture|startup|containerd|bwrap|packetimpact|iptables|nftables|packetdrill|language-directfs|language-goferfs|kubernetes|podman|website|go-export|workflows|syscalls|syscalls-save|syscalls-resume) ;;
    *) printf 'Unknown lane: %s\n' "$lane" >&2; usage >&2; exit 2 ;;
  esac
done
# The coordinator must be Linux; never start Bazel on the macOS host.
if [[ $(uname -s) != Linux ]]; then
  printf 'Run this command on a Linux remote coordinator.\n' >&2
  exit 2
fi
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

printf 'Selected remote lanes for Linux %s: %s\n' "$arch" "$*"
gaps

run_lane() {
  local lane=$1
  local command=test execution_config=rbe
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
    release-artifacts)
      command=build
      targets=(//debian:debian //debian:gvisor-release-tar-bz2 //debian:gvisor-release-tar-zstd)
      ;;
    release-repository)
      if [[ $arch != amd64 ]]; then
        printf 'Release repository tools run on AMD64 and check both package architectures.\n' >&2
        return 2
      fi
      targets=(//test/release:repository_test)
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
    startup)
      options=(--test_tag_filters=-requires-kvm)
      targets=(//test/benchmarks/base:startup_test_owned)
      ;;
    containerd)
      targets=(//test/root:crictl_test_owned)
      ;;
    bwrap)
      targets=(//runsc/cmd/alias/bwrap:bwrap_integration_test)
      ;;
    packetimpact)
      targets=(//test/packetimpact/tests:all_tests)
      ;;
    iptables|nftables|packetdrill)
      targets=("//test/$lane:owned_tests")
      ;;
    language-directfs|language-goferfs)
      if [[ $arch != amd64 ]]; then
        printf 'Language runtime images are declared only for AMD64.\n' >&2
        return 1
      fi
      options=(--test_timeout=1800
        "--test_env=RUNTIME_TESTS_FILTER=${RUNTIME_TESTS_FILTER:-}"
        "--test_env=RUNTIME_TESTS_PER_TEST_TIMEOUT=${RUNTIME_TESTS_PER_TEST_TIMEOUT:-20m}"
        "--test_env=RUNTIME_TESTS_RUNS_PER_TEST=${RUNTIME_TESTS_RUNS_PER_TEST:-1}"
        "--test_env=RUNTIME_TESTS_FLAKY_IS_ERROR=${RUNTIME_TESTS_FLAKY_IS_ERROR:-true}"
        "--test_env=RUNTIME_TESTS_FLAKY_SHORT_CIRCUIT=${RUNTIME_TESTS_FLAKY_SHORT_CIRCUIT:-true}")
      targets=("//test/runtimes:${lane#language-}_tests")
      ;;
    kubernetes)
      if [[ $arch != amd64 ]]; then
        printf 'The kind tool and node image are declared only for AMD64.\n' >&2
        return 1
      fi
      options=(--test_timeout=1800)
      targets=(//test/kubernetes/tests:kind_test)
      ;;
    podman)
      targets=(//test/podman:podman_test)
      ;;
    go-export)
      if [[ $arch != amd64 ]]; then
        printf 'The exported-module matrix runs on AMD64 workers.\n' >&2
        return 2
      fi
      targets=(//tools/go_export:all_test)
      ;;
    workflows)
      targets=(//:github_actions_test //:github_workflows_test //:buildkite_pipelines_test)
      ;;
    website)
      if [[ $arch != amd64 ]]; then
        printf 'The public website lane is qualified only for AMD64.\n' >&2
        return 2
      fi
      command=build
      targets=(//website:image)
      ;;
    syscalls|syscalls-save|syscalls-resume)
      options=(--target_pattern_file=test/syscalls.targets --cxxopt=-Werror)
      case "$lane" in
        syscalls) options+=('--test_tag_filters=-nogo,-allsave,-runsc_kvm,-runsc_slimvm') ;;
        syscalls-save) options+=(--test_tag_filters=save_restore) ;;
        syscalls-resume) options+=(--test_tag_filters=save_resume) ;;
      esac
      ;;
  esac
  if [[ $command == test ]]; then
    # The repository test consumes the public release packages, whose owning
    # build keeps Bazel's default stripping policy.
    if [[ $lane != release-repository ]]; then
      options+=(--strip=never)
    fi
    options+=(--incompatible_sandbox_hermetic_tmp=false --test_output=errors)
    if [[ $arch == arm64 ]]; then
      execution_config=rbe-arm64
      printf 'ARM64 Firecracker capacity remains unqualified; namespace-dependent tests require it.\n'
    fi
  fi
  bazel "$command" "--config=$execution_config" "--config=$architecture_config" \
    --keep_going "${options[@]}" "${targets[@]}"
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
