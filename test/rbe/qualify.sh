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
set +e
trap 'exit 130' INT
trap 'exit 143' TERM

lanes=(build-all plugin-build nogo unit unit-v1 container container-v1 smoke smoke-race release-artifacts release-repository cpu-images gpu-images docker docker-v1 overlay swgso hostnet plugin-network do root portforward posture startup benchmarks containerd bwrap fsstress packetimpact iptables nftables packetdrill language-directfs language-goferfs kubernetes podman syzkaller website go-export workflows lint lint-cc governance license-check license-headers python-distributions syscalls syscalls-save syscalls-resume)

usage() {
  cat <<'USAGE'
Usage: test/rbe/qualify.sh --header-base=REV amd64
       test/rbe/qualify.sh [--arch=amd64|arm64|all] [--header-base=REV] LANE [LANE ...]
       test/rbe/qualify.sh --list

Run Linux remote lanes using the configured Bazel RBE connection. The default
target architecture is AMD64. Tests use matching execution workers; builds
use AMD64 workers. This is partial public CI coverage;
selecting an architecture does not guarantee worker support. Existing failures
remain errors.
The all architecture selection combines unit and release-repository in one invocation.
The license-headers lane requires an explicit base and complete Git history.
USAGE
  printf '\nLanes: %s\n' "${lanes[*]}"
}

gaps() {
  cat <<'GAPS'
Unqualified by this profile: KVM and slimvm; the full ARM64 matrix; other cgroup
v1 lanes; the host systemd cgroup manager and alternate host kernels; the full
save/restore and coverage matrices; GPU/TPU runtime lanes; staged-binary consistency.
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
arch=amd64
header_base=
while (( $# > 0 )) && [[ $1 == --* ]]; do
  case "$1" in
    --arch=*) arch=${1#--arch=} ;;
    --header-base=*) header_base=${1#--header-base=} ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done
case "$arch" in
  amd64) architecture_config=x86_64 ;;
  arm64) architecture_config=aarch64 ;;
  all) architecture_config=x86_64 ;;
  *) printf 'Unknown architecture: %s\n' "$arch" >&2; exit 2 ;;
esac
if [[ $# == 1 && $1 == amd64 ]]; then
  if [[ $arch != amd64 ]]; then
    printf 'The amd64 profile requires --arch=amd64.\n' >&2
    exit 2
  fi
  set -- "${lanes[@]}"
fi
if (( $# == 0 )); then
  usage >&2
  exit 2
fi
# Validate every requested lane before starting any work.
for lane in "$@"; do
  if [[ $arch == all && $lane != unit && $lane != release-repository ]]; then
    printf 'The all architecture selection supports unit and release-repository.\n' >&2
    exit 2
  fi
  case "$lane" in
    build-all|plugin-build|nogo|unit|unit-v1|container|container-v1|smoke|smoke-race|release-artifacts|release-repository|cpu-images|gpu-images|docker|docker-v1|overlay|swgso|hostnet|plugin-network|do|root|portforward|posture|startup|benchmarks|containerd|bwrap|fsstress|packetimpact|iptables|nftables|packetdrill|language-directfs|language-goferfs|kubernetes|podman|syzkaller|website|go-export|workflows|lint|lint-cc|governance|license-check|license-headers|python-distributions|syscalls|syscalls-save|syscalls-resume) ;;
    *) printf 'Unknown lane: %s\n' "$lane" >&2; usage >&2; exit 2 ;;
  esac
done
# The coordinator must be Linux; never start Bazel on the macOS host.
if [[ $(uname -s) != Linux ]]; then
  printf 'Run this command on a Linux remote coordinator.\n' >&2
  exit 2
fi
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

# Resolve the caller's comparison base before any lane can modify generated
# files. A shallow checkout can give an incomplete merge base and selection.
for lane in "$@"; do
  if [[ $lane == license-headers ]]; then
    if [[ -z $header_base || $(git rev-parse --is-shallow-repository) != false ]]; then
      printf 'License headers require --header-base=REV and complete local Git history.\n' >&2
      exit 2
    fi
    header_base=$(git rev-parse --verify --end-of-options "$header_base^{commit}") || exit 2
    git merge-base "$header_base" HEAD >/dev/null || exit 2
    printf 'License header base: %s\n' "$header_base"
    break
  fi
done

printf 'Selected remote lanes for Linux %s: %s\n' "$arch" "$*"
gaps

# Make uses Bash, and lint.sh calls Bazel directly. Scope the same remote
# configuration to both paths without changing user rc files or credentials.
run_source_lane() (
  local lane=$1 qualification_rc go_root
  qualification_rc=$(mktemp)
  trap 'rm -f "$qualification_rc"' EXIT
  export qualification_rc
  printf '%s\n' 'build --config=rbe' 'build --config=x86_64' \
    'build --keep_going' > "$qualification_rc"
  bazel() {
    command bazel --bazelrc="$qualification_rc" "$@"
  }
  export -f bazel
  case "$lane" in
    lint)
      # Bootstrap the existing installer's Go resolver from the declared SDK;
      # lint.sh still owns the formatter version and canonical Go caches.
      go_root=$(bazel run @io_bazel_rules_go//go -- env GOROOT)
      if [[ $go_root != /* || $go_root == *$'\n'* || ! -x $go_root/bin/go ]]; then
        printf 'Declared Go SDK did not provide an executable absolute GOROOT: %s\n' "$go_root" >&2
        exit 1
      fi
      PATH="$go_root/bin:$PATH" make lint DOCKER_BUILD=false
      ;;
    lint-cc)
      make lint-cc DOCKER_BUILD=false
      ;;
    governance)
      make governance-check DOCKER_BUILD=false
      ;;
    license-check)
      make license-check DOCKER_BUILD=false
      ;;
  esac
)

# Preserve the canonical unit selection and add declared ARM variants and the
# existing release test to one graph. Release artifacts already select both CPUs.
run_platform_matrix() (
  set -e
  local selection_dir lane
  local -a options=()
  selection_dir=$(mktemp -d)
  trap 'rm -rf "$selection_dir"' EXIT
  : > "$selection_dir/targets"
  for lane in "$@"; do
    case "$lane" in
      unit)
        python3 test/rbe/unit_matrix.py query test/unit.targets > "$selection_dir/owners.query"
        bazel query --output=label --query_file="$selection_dir/owners.query" > "$selection_dir/owners"
        python3 test/rbe/unit_matrix.py actions "$selection_dir/owners" > "$selection_dir/actions.query"
        bazel aquery --config=rbe-matrix --config=x86_64 --output=jsonproto --include_artifacts=false \
          --query_file="$selection_dir/actions.query" > "$selection_dir/actions.json"
        python3 test/rbe/unit_matrix.py select test/unit.targets "$selection_dir/owners" \
          "$selection_dir/actions.json" "$selection_dir/unit-targets"
        cat "$selection_dir/unit-targets" >> "$selection_dir/targets"
        options+=(--config=unit --strip=never)
        ;;
      release-repository)
        printf '%s\n' '//test/release:repository_test' >> "$selection_dir/targets"
        ;;
    esac
  done
  bazel test --config=rbe-matrix --config=x86_64 --keep_going \
    --incompatible_sandbox_hermetic_tmp=false --test_output=errors "${options[@]}" \
    --target_pattern_file="$selection_dir/targets"
)

run_lane() (
  # Called as a plain command below so errexit also applies inside helpers.
  # The parent records failures and continues with other requested lanes.
  set -e
  local lane=$1
  local command=test execution_config=rbe
  local -a options=() targets=()
  if [[ $lane == unit-v1 || $lane == container-v1 || $lane == docker-v1 ]]; then
    if [[ $arch != amd64 ]]; then
      printf 'The public cgroup-v1 lanes are declared for AMD64.\n' >&2
      return 2
    fi
    execution_config=rbe-cgroup-v1
  fi
  case "$lane" in
    build-all)
      command=build
      options=(--build_tag_filters=-network_plugins)
      targets=(//...)
      ;;
    plugin-build)
      if [[ $arch != amd64 ]]; then
        printf 'The public plugin build is declared for AMD64.\n' >&2
        return 2
      fi
      command=build
      options=(-c opt --config=plugin-tldk)
      targets=(//runsc:runsc-plugin-stack)
      ;;
    lint|lint-cc|governance|license-check)
      if [[ $arch != amd64 ]]; then
        printf 'Hosted source tools are qualified only on the AMD64 coordinator.\n' >&2
        return 2
      fi
      run_source_lane "$lane"
      return "$?"
      ;;
    license-headers)
      tools/check_license_headers.sh "$header_base"
      return "$?"
      ;;
    python-distributions)
      command=build
      targets=(//sandboxexec/sandbox/python:dist)
      ;;
    nogo)
      options=(--config=nogo)
      targets=(//...)
      ;;
    unit|unit-v1)
      # test/unit.targets also retains non-test build targets and the existing
      # exclusions. Keep its selection separate from Nogo's positive tag filter.
      options=(--config=unit)
      ;;
    container|container-v1)
      if [[ $arch != amd64 ]]; then
        printf 'The public container lanes are declared for AMD64.\n' >&2
        return 2
      fi
      # Unlike unit, the public container selection includes KVM tests.
      options=(--test_tag_filters=-nogo)
      if [[ $lane == container ]]; then
        options+=(--test_env=CGROUPV2=true)
      fi
      targets=(//runsc/container/...)
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
    cpu-images|gpu-images)
      targets=("//test/docker:${lane%-images}_image_sources_${arch}_test")
      ;;
    docker|docker-v1)
      options=(--config=docker)
      targets=(//test/docker:owned_tests)
      ;;
    overlay|swgso|hostnet)
      targets=("//test/docker:${lane}_tests")
      ;;
    plugin-network)
      if [[ $arch != amd64 ]]; then
        printf 'The public plugin network test is declared for AMD64.\n' >&2
        return 2
      fi
      options=(--config=plugin-tldk)
      targets=(//test/docker:plugin_network_tests)
      ;;
    do)
      if [[ $arch != amd64 ]]; then
        printf 'The public do smoke checks are declared for AMD64.\n' >&2
        return 2
      fi
      targets=(//:do_tests)
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
    benchmarks)
      if [[ $arch != amd64 ]]; then
        printf 'Continuous CI benchmarks are declared for AMD64; ARM64 workers remain unqualified.\n' >&2
        return 2
      fi
      # CI reports these jobs as soft failures. Keep their status visible here;
      # --keep_going still collects the other complete benchmark workloads.
      options=(--test_tag_filters=-requires-kvm)
      targets=(//test/benchmarks:continuous_tests)
      ;;
    containerd)
      targets=(//test/root:crictl_test_owned)
      ;;
    bwrap)
      targets=(//runsc/cmd/alias/bwrap:bwrap_integration_test)
      ;;
    fsstress)
      targets=(//test/fsstress:fsstress_test_owned)
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
    syzkaller)
      if [[ $arch != amd64 ]]; then
        printf 'The Syzkaller smoke test is declared only for AMD64.\n' >&2
        return 2
      fi
      targets=(//test/syzkaller:smoke_test)
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
        syscalls)
          if [[ $arch == arm64 ]]; then
            options+=(--config=syscalls-arm64)
          else
            # RBE also excludes KVM; test_tag_filters replaces rather than
            # extends the public syscalls-amd64 config's selection.
            options+=('--test_tag_filters=-nogo,-allsave,-runsc_kvm,-runsc_slimvm')
          fi
          ;;
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
)

if [[ $arch == all ]]; then
  run_platform_matrix "$@"
  exit "$?"
fi

status=0
for lane in "$@"; do
  printf '\nRunning lane: %s\n' "$lane"
  run_lane "$lane"
  lane_status=$?
  printf 'Lane %s exited %d\n' "$lane" "$lane_status"
  if (( lane_status != 0 )); then
    status=1
  fi
done
exit "$status"
