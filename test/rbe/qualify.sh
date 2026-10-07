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

lanes=(build-all presubmit-build plugin-build nogo unit unit-v1 container container-v1 smoke smoke-race release-artifacts release-repository cpu-images gpu-images cos-metadata docker docker-v1 overlay swgso hostnet plugin-network 'do' root portforward posture startup benchmarks containerd bwrap fsstress packetimpact iptables nftables packetdrill language-directfs language-goferfs kubernetes podman syzkaller website go-export codeql workflows lint lint-cc governance license-check license-headers python-distributions syscalls syscalls-kvm syscalls-save syscalls-resume)

usage() {
  cat <<'USAGE'
Usage: test/rbe/qualify.sh --header-base=REV amd64
       test/rbe/qualify.sh [--arch=amd64|arm64|all] [--test-execution=remote|local] [--syscall-bucket=0..14] [--benchmark-target=LABEL] [--header-base=REV] LANE [LANE ...]
       test/rbe/qualify.sh --list

Run Linux remote lanes using the configured Bazel RBE connection. The default
target architecture is AMD64. Tests use matching execution workers. Builds
prefer AMD64 workers while retaining declared native generator requirements.
This is partial public CI coverage;
selecting an architecture does not guarantee worker support. Existing failures
remain errors.
The all architecture selection combines unit, release-repository and syscalls
with the target-configured test lanes described in test/rbe/README.md.
Presubmit builds run separately for each requested CPU in the same job.
Clang-tidy retains a separate AMD64 aspect build over its recursive roots.
ARM64 selection follows the public unit, syscall, smoke, Docker, bwrap and
image-source lanes; unavailable workers are reported before execution.
The license-headers lane requires an explicit base and complete Git history.
The cos-metadata lane requires COS_IMAGES_JSON with the complete gcloud catalog.
Local execution supports smoke, bwrap, ordinary syscalls, ARM64 unit/resume tests
and AMD64 KVM syscalls and startup/posture/portforward/root/benchmarks.
Compilation remains remote. Hybrid profiles run in one invocation: native namespace owners run
locally; ordinary native and shared owners run remotely.
An optional syscall bucket selects one existing hash15 partition, not the full
profile. Its report retains every unexecuted bucket owner.
A benchmark target selects one member of the continuous suite, retaining its
original workload and timeout. Other suite members remain unexecuted.
USAGE
  printf '\nLanes: %s\n' "${lanes[*]}"
}

gaps() {
  cat <<'GAPS'
Environment limits: this profile does not supply KVM, slimvm, ARM64
Firecracker, alternate kernels, or GPU/TPU runtime environments.
Selecting a lane does not establish a passing result or full public CI coverage.
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
test_execution=remote
syscall_bucket=
benchmark_target=
header_options=()
header_base=
while (( $# > 0 )) && [[ $1 == --* ]]; do
  case "$1" in
    --arch=*) arch=${1#--arch=} ;;
    --test-execution=*) test_execution=${1#--test-execution=} ;;
    --syscall-bucket=*) syscall_bucket=${1#--syscall-bucket=} ;;
    --benchmark-target=*) benchmark_target=${1#--benchmark-target=} ;;
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
case "$test_execution" in
  remote) ;;
  local)
    case "$arch:$(uname -m)" in
      amd64:x86_64|arm64:aarch64) ;;
      *) printf 'Local tests require a single matching host architecture.\n' >&2; exit 2 ;;
    esac
    if (( $# != 1 )); then
      printf 'Select one lane for local tests.\n' >&2
      exit 2
    fi
    case "$1:$arch" in
      smoke:*|bwrap:*|unit:arm64|docker:arm64|cpu-images:arm64|gpu-images:arm64|syscalls:*|syscalls-resume:arm64|syscalls-kvm:amd64|startup:amd64|posture:amd64|portforward:amd64|root:amd64|benchmarks:amd64) ;;
      *) printf 'Local tests support smoke, bwrap, ordinary syscalls, ARM64 unit/resume/Docker/image profiles and AMD64 KVM syscalls/startup/posture/portforward/root/benchmarks.\n' >&2; exit 2 ;;
    esac
    ;;
  *) printf 'Unknown test execution: %s\n' "$test_execution" >&2; exit 2 ;;
esac
if [[ -n $syscall_bucket ]]; then
  if [[ ! $syscall_bucket =~ ^([0-9]|1[0-4])$ || $test_execution != local || $# != 1 || ( $arch:${1:-} != arm64:syscalls && $arch:${1:-} != arm64:syscalls-resume && $arch:${1:-} != amd64:syscalls-kvm ) ]]; then
    printf 'A syscall bucket must be 0..14 and requires local ARM64 syscalls/syscalls-resume or AMD64 syscalls-kvm.\n' >&2
    exit 2
  fi
fi
if [[ -n $benchmark_target && ( $arch != amd64 || $test_execution != local || $# != 1 || ${1:-} != benchmarks ) ]]; then
  printf 'A benchmark target requires local AMD64 benchmarks.\n' >&2
  exit 2
fi
if [[ $# == 1 && $1 == amd64 ]]; then
  if [[ $arch != amd64 ]]; then
    printf 'The amd64 profile requires --arch=amd64.\n' >&2
    exit 2
  fi
  set --
  for lane in "${lanes[@]}"; do
    # KVM syscall execution belongs to the separate local-host profile.
    if [[ $lane != syscalls-kvm ]]; then set -- "$@" "$lane"; fi
  done
fi
if (( $# == 0 )); then
  usage >&2
  exit 2
fi
# Validate every requested lane before starting any work.
for lane in "$@"; do
  if [[ $lane == syscalls-kvm && ( $test_execution != local || $arch != amd64 ) ]]; then
    printf 'The KVM syscall lane requires local AMD64 execution.\n' >&2
    exit 2
  fi
  if [[ $arch == all ]]; then
    case "$lane" in
      presubmit-build|nogo|unit|unit-v1|container|container-v1|docker-v1|release-artifacts|release-repository|python-distributions|website|syscalls|syscalls-save|syscalls-resume|smoke|smoke-race|plugin-build|plugin-network|do|docker|root|portforward|bwrap|workflows|lint|language-directfs|language-goferfs|overlay|swgso|hostnet|containerd|fsstress|packetimpact|iptables|nftables|packetdrill|kubernetes|podman|syzkaller|go-export|codeql|cpu-images|gpu-images|cos-metadata|posture|startup|benchmarks|governance|license-headers|lint-cc) ;;
      *) printf 'Lane %s does not support the all architecture selection.\n' "$lane" >&2; exit 2 ;;
    esac
  fi
  case "$lane" in
    build-all|presubmit-build|plugin-build|nogo|unit|unit-v1|container|container-v1|smoke|smoke-race|release-artifacts|release-repository|cpu-images|gpu-images|cos-metadata|docker|docker-v1|overlay|swgso|hostnet|plugin-network|do|root|portforward|posture|startup|benchmarks|containerd|bwrap|fsstress|packetimpact|iptables|nftables|packetdrill|language-directfs|language-goferfs|kubernetes|podman|syzkaller|website|go-export|codeql|workflows|lint|lint-cc|governance|license-check|license-headers|python-distributions|syscalls|syscalls-kvm|syscalls-save|syscalls-resume) ;;
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
    header_head=$(git rev-parse --verify HEAD) || exit 2
    git merge-base "$header_base" "$header_head" >/dev/null || exit 2
    header_options=("--repo_env=GVISOR_HEADER_BASE=$header_base" "--repo_env=GVISOR_HEADER_HEAD=$header_head")
    printf 'License header base: %s\n' "$header_base"
    break
  fi
done

printf 'Selected lanes for Linux %s (%s tests): %s\n' "$arch" "$test_execution" "$*"
if [[ $test_execution == remote ]]; then
  gaps
fi

# Scope remote configuration to direct Bazel calls and Make's Bash recipes
# without changing user rc files or credentials.
run_license_check() (
  local qualification_rc
  qualification_rc=$(mktemp)
  trap 'rm -f "$qualification_rc"' EXIT
  export qualification_rc
  printf '%s\n' 'build --config=rbe' 'build --config=x86_64' \
    'build --keep_going' > "$qualification_rc"
  bazel() {
    command bazel --bazelrc="$qualification_rc" "$@"
  }
  export -f bazel
  make license-check DOCKER_BUILD=false
)

# Set the caller's targets array from the same owning suites for standalone and
# combined invocations.
shared_test_targets() {
  local target_arch=$2
  case "$1" in
    do) targets=(//:do_tests) ;;
    docker) targets=(//test/docker:owned_tests) ;;
    plugin-network) targets=(//test/docker:plugin_network_tests) ;;
    root)
      targets=(//test/root:root_test_owned)
      if [[ $target_arch == amd64 ]]; then
        targets+=(//test/rbe:systemd_fixture_test)
      fi
      ;;
    posture) targets=(//test/root:sandbox_posture_test_owned) ;;
    startup) targets=(//test/benchmarks/base:startup_test_owned) ;;
    benchmarks) targets=(//test/benchmarks:continuous_tests) ;;
    portforward) targets=(//test/root:portforward_test_owned) ;;
    bwrap) targets=(//runsc/cmd/alias/bwrap:bwrap_integration_test) ;;
    license-headers) targets=(//tools:license_headers_test) ;;
    workflows) targets=(//:github_actions_test //:github_workflows_test //:buildkite_pipelines_test) ;;
    governance) targets=(//:governance-check) ;;
    lint) targets=(//tools/lint:lint_tests) ;;
    overlay|swgso|hostnet) targets=("//test/docker:${1}_tests") ;;
    containerd) targets=(//test/root:crictl_test_owned) ;;
    fsstress) targets=(//test/fsstress:fsstress_test_owned) ;;
    packetimpact) targets=(//test/packetimpact/tests:all_tests) ;;
    iptables|nftables|packetdrill) targets=("//test/$1:owned_tests") ;;
    kubernetes) targets=(//test/kubernetes/tests:kind_test) ;;
    podman) targets=(//test/podman:podman_test) ;;
    syzkaller) targets=(//test/syzkaller:smoke_test) ;;
    go-export) targets=(//tools/go_export:all_test) ;;
    cpu-images|gpu-images) targets=("//test/docker:${1%-images}_image_sources_${target_arch}_test") ;;
    cos-metadata) targets=(//test/gpu:cos_gpu_compatibility_test) ;;
    language-directfs|language-goferfs) targets=("//test/runtimes:${1#language-}_tests") ;;
    *) printf 'Unknown shared test lane: %s\n' "$1" >&2; return 2 ;;
  esac
}

# The runtime runner owns defaults. Forward only explicit caller settings,
# including empty values, through Bazel's client environment.
language_test_options() {
  local name
  for name in RUNTIME_TESTS_FILTER RUNTIME_TESTS_PER_TEST_TIMEOUT RUNTIME_TESTS_RUNS_PER_TEST RUNTIME_TESTS_FLAKY_IS_ERROR RUNTIME_TESTS_FLAKY_SHORT_CIRCUIT; do
    if [[ ${!name+x} ]]; then
      options+=("--test_env=$name")
    fi
  done
}

# Docker owns the PID/cgroup/network namespaces; the declared fixture prepares
# their scratch and controllers. Compilation retains the remote-only strategy.
docker_test_options() {
  options+=(
    --local_test_jobs=2
    --experimental_enable_docker_sandbox
    --experimental_docker_privileged
    --noexperimental_docker_use_customized_images
    --noincompatible_legacy_local_fallback
    --sandbox_default_allow_network=false
    --test_env=GO_TEST_WRAP_TESTV=1
    "--test_env=GVISOR_HOST_CGROUP_NS=$(readlink /proc/self/ns/cgroup)"
    "--test_env=GVISOR_HOST_PID_NS=$(readlink /proc/self/ns/pid)"
  )
}

# A file keeps large ordered target lists below Linux's argument-size limit.
# Aquery ignores target_pattern_file; apply this config after the caller's options.
analyze_profile() {
  local patterns=$1 events=$2
  shift 2
  local rc=$events.bazelrc
  python3 test/rbe/unit_matrix.py universe-rc "$patterns" > "$rc"
  bazel "--bazelrc=$rc" aquery "$@" "${header_options[@]}" --config=rbe-selection \
    "--build_event_json_file=$events" 'set()'
}

# Let Bazel select each public profile before checking worker capacity.
# Aquery inherits build options, so the canonical loading filters live there;
# test inherits the same options. Its ordered universe uses the owning roots.
select_test_profile() {
  local selection_dir=$1 lane=$2 target_arch=$3 roots=$4 target_config=x86_64 prefix
  shift 4
  local -a selection_options=() page_size_options=() routing_options=() variant_options=()
  prefix=$selection_dir/$lane-$target_arch
  if [[ $target_arch == arm64 ]]; then
    target_config=aarch64
  fi
  if [[ $lane == syscalls* ]]; then
    selection_options=(--syscall-policy)
  fi
  if [[ $lane == syscalls-64k ]]; then
    page_size_options=(--page-size=64k)
  fi
  if [[ $test_execution == local ]]; then
    routing_options=("--//tools/bazeldefs:local_test_architecture=$target_arch")
    variant_options=(--hybrid)
    selection_options+=(--hybrid)
    if [[ $lane == syscalls-kvm ]]; then
      variant_options+=(--kvm-only)
      selection_options+=(--kvm-only)
    elif [[ $lane == syscalls && $target_arch == amd64 ]]; then
      routing_options+=(--//tools/bazeldefs:local_test_backend=docker)
    fi
    if [[ -n $syscall_bucket ]]; then
      selection_options+=("--syscall-bucket=$syscall_bucket")
    fi
  fi
  analyze_profile "$roots" "$prefix-profile.json" \
    --config=rbe-matrix "--config=$target_config" "$@" --build_tests_only
  python3 test/rbe/unit_matrix.py profile-actions \
    "$prefix-profile.json" "$target_arch" "${page_size_options[@]}" "${variant_options[@]}" > "$prefix.query"
  bazel aquery --config=rbe-matrix --config=x86_64 --build_tests_only \
    "${routing_options[@]}" \
    --output=jsonproto --include_artifacts=false \
    "--build_event_json_file=$prefix-routing.json" \
    "--query_file=$prefix.query" > "$prefix-actions.json"
  python3 test/rbe/unit_matrix.py select-profile \
    "$prefix-profile.json" "$target_arch" "$prefix-routing.json" \
    "$prefix-actions.json" "$prefix-targets" "${selection_options[@]}" "${page_size_options[@]}"
}

select_syscall_profile() {
  local selection_dir=$1 lane=$2 syscall_arch=$3
  local -a profile_options=()
  case "$lane" in
    syscalls|syscalls-kvm) profile_options=("--config=syscalls-$syscall_arch") ;;
    syscalls-64k) profile_options=(--config=syscalls-arm64-64k) ;;
    syscalls-save) profile_options=(--test_tag_filters=save_restore) ;;
    syscalls-resume) profile_options=(--test_tag_filters=save_resume) ;;
    *) printf 'Unknown syscall profile: %s\n' "$lane" >&2; return 2 ;;
  esac
  select_test_profile "$selection_dir" "$lane" "$syscall_arch" \
    test/syscalls.targets "${profile_options[@]}"
}

# Expand the public unit selection before removing its lane-wide filters from
# a mixed cgroup invocation. A separate analysis retains non-test build roots;
# filtered tests' build-only work remains in standalone unit/build-all lanes.
select_unit_profile() {
  local selection_dir=$1
  if [[ -f $selection_dir/unit-profile.json ]]; then
    return
  fi
  analyze_profile test/unit.targets "$selection_dir/unit-profile.json" \
    --config=rbe-matrix --config=x86_64 --config=unit --strip=never --build_tests_only
  analyze_profile test/unit.targets "$selection_dir/unit-build-profile.json" \
    --config=rbe-matrix --config=x86_64 --config=unit --strip=never --nobuild_tests_only
  python3 test/rbe/unit_matrix.py build-roots "$selection_dir/unit-build-profile.json" \
    > "$selection_dir/unit-build-targets"
  cat "$selection_dir/unit-build-targets" >> "$selection_dir/targets"
}

# Reuse the native graph and canonical profile in one invocation. The
# frontend owns local namespace requirements; ordinary/shared tests stay remote.
run_hybrid_profile() (
  set -e
  local lane=$1 selection_dir
  local -a lane_options=() options=()
  selection_dir=$(mktemp -d)
  trap 'rm -rf "$selection_dir"' EXIT
  if [[ $lane == unit ]]; then
    python3 test/rbe/unit_matrix.py query test/unit.targets > "$selection_dir/owners.query"
    bazel query --output=label --query_file="$selection_dir/owners.query" > "$selection_dir/owners"
    python3 test/rbe/unit_matrix.py actions "$selection_dir/owners" > "$selection_dir/actions.query"
    bazel aquery --config=rbe-matrix --config=x86_64 \
      --//tools/bazeldefs:local_test_architecture=arm64 --output=jsonproto --include_artifacts=false \
      --query_file="$selection_dir/actions.query" > "$selection_dir/actions.json"
    analyze_profile test/unit.targets "$selection_dir/profile.json" \
      --config=rbe-matrix --config=aarch64 --config=unit --strip=never --build_tests_only
    python3 test/rbe/unit_matrix.py select test/unit.targets "$selection_dir/owners" \
      "$selection_dir/actions.json" "$selection_dir/targets" --profile "$selection_dir/profile.json" --hybrid \
      | tee "$selection_dir/selection.json"
  else
    lane_options=(--cxxopt=-Werror)
    select_syscall_profile "$selection_dir" "$lane" "$arch" | tee "$selection_dir/selection.json"
    cp "$selection_dir/$lane-$arch-targets" "$selection_dir/targets"
    cp "$selection_dir/$lane-$arch-actions.json" "$selection_dir/actions.json"
    cp "$selection_dir/$lane-$arch-profile.json" "$selection_dir/profile.json"
  fi
  # Preserve the selection, but never upload Bazel's parsed credential options.
  mkdir -p "${RUNNER_TEMP:?}/qualification/$lane-selection"
  cp "$selection_dir/selection.json" "$selection_dir/actions.json" \
    "$selection_dir/targets" "$RUNNER_TEMP/qualification/$lane-selection/"
  if [[ -f $selection_dir/owners ]]; then
    cp "$selection_dir/owners" "$RUNNER_TEMP/qualification/$lane-selection/"
  fi
  python3 - "$selection_dir" "$RUNNER_TEMP/qualification/$lane-selection" <<'PY'
import json
from pathlib import Path
import sys

keys = {"id", "children", "configured", "finished", "aborted"}
for source in Path(sys.argv[1]).glob("*.json"):
    if source.name == "profile.json" or source.name.endswith("-routing.json"):
        with (Path(sys.argv[2]) / source.name).open("w") as output:
            for line in source.read_text().splitlines():
                event = json.loads(line)
                output.write(json.dumps({key: value for key, value in event.items() if key in keys}) + "\n")
PY
  # Disposable qualification intersection; the complete maintained selection
  # above remains in the artifact, and the source interface is unchanged.
  [[ $arch == amd64 && $lane == syscalls && -z $syscall_bucket ]]
  python3 - "$selection_dir/targets" "$RUNNER_TEMP/qualification/$lane-selection" <<'PY_FOCUS'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
artifacts = Path(sys.argv[2])
canonical = set(path.read_text().splitlines())
selected = set([
    "//test/syscalls:cgroup2_test_native_amd64",
    "//test/syscalls:iptables_test_native_amd64",
    "//test/syscalls:mount_fd_test_native_amd64",
    "//test/syscalls:openat2_test_native_amd64",
    "//test/syscalls:pty_test_native_amd64",
    "//test/syscalls:socket_inet_loopback_isolated_test_native_amd64",
    "//test/syscalls:socket_inet_loopback_isolated_test_runsc_systrap_hostnet_amd64",
    "//test/syscalls:socket_inet_loopback_test_native_amd64",
    "//test/syscalls:socket_inet_loopback_test_runsc_systrap_hostnet_amd64",
    "//test/syscalls:socket_netlink_netfilter_test_native_amd64",
    "//test/syscalls:tcp_socket_test_native_amd64"
])
assert selected <= canonical, sorted(selected - canonical)
text = "".join(label + "\n" for label in sorted(selected))
(artifacts / "focused-targets").write_text(text)
(artifacts / "unexecuted-targets").write_text("".join(label + "\n" for label in sorted(canonical - selected)))
(artifacts / "focused-selection.json").write_text(json.dumps({
    "complete_profile_owners": sorted(canonical),
    "selected_owners": sorted(selected),
    "unexecuted_owners": sorted(canonical - selected),
    "scope": "Eleven complete residual owners; original 102 failed cases are a subset of their current complete suites.",
}, indent=2) + "\n")
path.write_text(text)
PY_FOCUS
  printf '%s %s profile: namespace/KVM owners run locally; ordinary native and shared owners run remotely in the same invocation.\n' "$arch" "$lane"
  if [[ -n $syscall_bucket ]]; then
    printf 'Running syscall hash15 bucket %s only; the other buckets remain unexecuted.\n' "$syscall_bucket"
  fi
  if [[ $lane == syscalls && $arch == amd64 ]]; then
    docker_test_options
    options+=("--strategy=TestRunner=remote,docker" --//tools/bazeldefs:local_test_backend=docker)
  fi
  bazel test --config=rbe --config=x86_64 --config=rbe-hybrid-tests --keep_going \
    "--//tools/bazeldefs:local_test_architecture=$arch" \
    --strip=never --incompatible_sandbox_hermetic_tmp=false --test_output=errors \
    --test_env=GO_TEST_WRAP_TESTV=1 "${lane_options[@]}" "${options[@]}" --target_pattern_file="$selection_dir/targets"
)

select_cgroup_profile() {
  local selection_dir=$1 lane=$2 profile
  local -a profile_options=() targets=()
  if [[ $lane == unit-v1 ]]; then
    select_unit_profile "$selection_dir"
    python3 test/rbe/unit_matrix.py cgroup-targets "$selection_dir/unit-profile.json" \
      >> "$selection_dir/explicit-targets"
    return
  fi
  case "$lane" in
    container|container-v1)
      targets=(//runsc/container/...)
      profile_options=(--test_tag_filters=-nogo)
      ;;
    docker-v1) shared_test_targets docker amd64 ;;
  esac
  printf '%s\n' "${targets[@]}" > "$selection_dir/$lane-roots"
  analyze_profile "$selection_dir/$lane-roots" "$selection_dir/$lane-profile.json" \
    --config=rbe-matrix --config=x86_64 --strip=never "${profile_options[@]}" --build_tests_only
  profile=$selection_dir/$lane-profile.json
  if [[ $lane == container || $lane == container-v1 ]]; then
    # Replace the aggregate runtime owner before applying worker policy. Keep
    # every other public owner, including future additions to the package.
    python3 test/rbe/unit_matrix.py container-platform-targets "$profile" \
      > "$selection_dir/$lane-platform-roots"
    profile=$selection_dir/$lane-platform-profile.json
    analyze_profile "$selection_dir/$lane-platform-roots" "$profile" \
      --config=rbe-matrix --config=x86_64 --strip=never --build_tests_only \
      --test_tag_filters=-nogo,-requires-kvm
    python3 test/rbe/unit_matrix.py kvm-query "$selection_dir/$lane-platform-roots" \
      > "$selection_dir/$lane-kvm.query"
    bazel query --output=label --query_file="$selection_dir/$lane-kvm.query" \
      > "$selection_dir/$lane-kvm-targets"
    python3 test/rbe/unit_matrix.py select-filtered "$profile" \
      "$selection_dir/$lane-kvm-targets" "$selection_dir/$lane-platform-targets"
  fi
  if [[ $lane == container ]]; then
    python3 test/rbe/unit_matrix.py container-targets "$profile" \
      >> "$selection_dir/explicit-targets"
  else
    python3 test/rbe/unit_matrix.py cgroup-targets "$profile" \
      >> "$selection_dir/explicit-targets"
  fi
}

# Ordinary unit/syscall combinations retain their canonical build-only roots.
# Mixing cgroup profiles uses explicit canonical tests so lane-wide filters do
# not suppress another requested lane's owners. Release owns both CPU builds.
run_platform_matrix() (
  set -e
  local selection_dir lane command=build include_unit=false include_syscalls=false include_checkpoints=false include_nogo=false include_public_profiles=false explicit_unit=false
  local -a options=() targets=() verification_options=() unit_selection_options=()
  selection_dir=$(mktemp -d)
  trap 'rm -rf "$selection_dir"' EXIT
  : > "$selection_dir/targets"
  : > "$selection_dir/explicit-targets"
  : > "$selection_dir/shared-targets"
  : > "$selection_dir/filtered-targets"
  for lane in "$@"; do
    case "$lane" in
      plugin-build|release-artifacts|python-distributions|website|codeql) ;;
      *) command='test' ;;
    esac
    case "$lane" in
      unit) include_unit=true ;;
      unit-v1|container|container-v1|docker-v1) explicit_unit=true ;;
      syscalls-save|syscalls-resume) include_checkpoints=true ;;
      nogo) include_nogo=true ;;
    esac
  done
  for lane in "$@"; do
    case "$lane" in
      nogo)
        # Each existing Nogo owner already analyzes both architectures. Select
        # its leaves without applying the positive tag filter to other lanes.
        bazel aquery --config=rbe-matrix --config=x86_64 --config=nogo \
          --universe_scope=//... \
          "--build_event_json_file=$selection_dir/nogo-profile.json" 'set()'
        python3 test/rbe/unit_matrix.py profile-targets "$selection_dir/nogo-profile.json" amd64 \
          >> "$selection_dir/explicit-targets"
        ;;
      unit)
        python3 test/rbe/unit_matrix.py query test/unit.targets > "$selection_dir/owners.query"
        bazel query --output=label --query_file="$selection_dir/owners.query" > "$selection_dir/owners"
        python3 test/rbe/unit_matrix.py actions "$selection_dir/owners" > "$selection_dir/actions.query"
        bazel aquery --config=rbe-matrix --config=x86_64 --output=jsonproto --include_artifacts=false \
          --query_file="$selection_dir/actions.query" > "$selection_dir/actions.json"
        if [[ $explicit_unit == true ]]; then
          select_unit_profile "$selection_dir"
          unit_selection_options=(--profile "$selection_dir/unit-profile.json")
        fi
        python3 test/rbe/unit_matrix.py select test/unit.targets "$selection_dir/owners" \
          "$selection_dir/actions.json" "$selection_dir/unit-targets" "${unit_selection_options[@]}"
        if [[ $explicit_unit == true ]]; then
          cat "$selection_dir/unit-targets" >> "$selection_dir/explicit-targets"
        else
          cat "$selection_dir/unit-targets" >> "$selection_dir/targets"
          options+=(--config=unit)
        fi
        options+=(--strip=never)
        if [[ $include_nogo == true && $explicit_unit == false ]]; then
          # Preserve the original unit selection before allowing Nogo through
          # its wildcard roots. Verify the complete union before execution.
          analyze_profile "$selection_dir/unit-targets" "$selection_dir/unit-profile.json" \
            --config=rbe-matrix --config=x86_64 --config=unit --strip=never --build_tests_only
          verification_options+=(--profile "$selection_dir/unit-profile.json")
        fi
        ;;
      unit-v1|container|container-v1|docker-v1)
        select_cgroup_profile "$selection_dir" "$lane"
        options+=(--strip=never)
        ;;
      release-repository)
        printf '%s\n' '//test/release:repository_test' >> "$selection_dir/explicit-targets"
        ;;
      release-artifacts)
        printf '%s\n' '//test/release:artifacts' >> "$selection_dir/targets"
        ;;
      python-distributions)
        printf '%s\n' '//sandboxexec/sandbox/python:dist' >> "$selection_dir/targets"
        ;;
      website)
        printf '%s\n' '//website:artifact' >> "$selection_dir/targets"
        ;;
      syscalls)
        include_syscalls=true
        select_syscall_profile "$selection_dir" "$lane" amd64
        select_syscall_profile "$selection_dir" "$lane" arm64
        select_syscall_profile "$selection_dir" syscalls-64k arm64
        if [[ $include_unit == true || $explicit_unit == true || $include_checkpoints == true ]]; then
          # Global -allsave would discard the explicitly requested checkpoint
          # profiles. Use their separately selected owners in a combined run.
          cat "$selection_dir/syscalls-amd64-targets" >> "$selection_dir/explicit-targets"
        else
          # Keep the existing standalone syscall roots and RBE exclusions,
          # including their build-only targets. The ARM additions were selected
          # by the separate public ARM profile above.
          cat test/syscalls.targets >> "$selection_dir/targets"
          options+=(--config=syscalls-amd64 '--test_tag_filters=-nogo,-allsave,-runsc_kvm,-runsc_slimvm' --strip=never)
          if [[ $include_nogo == true ]]; then
            cat "$selection_dir/syscalls-amd64-targets" >> "$selection_dir/explicit-targets"
          fi
        fi
        cat "$selection_dir/syscalls-arm64-targets" >> "$selection_dir/explicit-targets"
        cat "$selection_dir/syscalls-64k-arm64-targets" >> "$selection_dir/explicit-targets"
        ;;
      syscalls-save|syscalls-resume)
        include_syscalls=true
        # Match the public continuous jobs' architectures.
        local checkpoint_arch=amd64
        if [[ $lane == syscalls-resume ]]; then
          checkpoint_arch=arm64
        fi
        select_syscall_profile "$selection_dir" "$lane" "$checkpoint_arch"
        cat "$selection_dir/$lane-$checkpoint_arch-targets" >> "$selection_dir/explicit-targets"
        options+=(--strip=never)
        ;;
      smoke|docker|bwrap|cpu-images|gpu-images)
        include_public_profiles=true
        # These public lanes run on both CPUs. Expand each owning suite under
        # that architecture before mapping its declared test variants.
        local profile_arch
        for profile_arch in amd64 arm64; do
          if [[ $lane == smoke ]]; then
            targets=(//:release_smoke_test)
          else
            shared_test_targets "$lane" "$profile_arch"
          fi
          printf '%s\n' "${targets[@]}" > "$selection_dir/$lane-$profile_arch-roots"
          select_test_profile "$selection_dir" "$lane" "$profile_arch" \
            "$selection_dir/$lane-$profile_arch-roots" --strip=never
          cat "$selection_dir/$lane-$profile_arch-targets" >> "$selection_dir/explicit-targets"
        done
        options+=(--strip=never)
        ;;
      smoke-race)
        printf '%s\n' '//:release_smoke_race_test' >> "$selection_dir/explicit-targets"
        options+=(--strip=never)
        ;;
      posture|startup|benchmarks)
        shared_test_targets "$lane" amd64
        printf '%s\n' "${targets[@]}" >> "$selection_dir/filtered-targets"
        printf 'Combined lane %s retains AMD64 execution; no ARM64 coverage is added.\n' "$lane"
        options+=(--strip=never)
        ;;
      codeql)
        printf '%s\n' '//tools/codeql:all' >> "$selection_dir/targets"
        ;;
      plugin-build)
        # This is a build-only root, not an executable test for the loading
        # verifier. Its own transition preserves opt/strip=sometimes.
        printf '%s\n' '//runsc:runsc-plugin-stack-build' >> "$selection_dir/targets"
        ;;
      plugin-network|do|root|portforward|workflows|lint|governance|language-directfs|language-goferfs|overlay|swgso|hostnet|containerd|fsstress|packetimpact|iptables|nftables|packetdrill|kubernetes|podman|syzkaller|go-export|cos-metadata|license-headers)
        shared_test_targets "$lane" amd64
        if [[ $lane == language-* ]]; then
          language_test_options
        fi
        printf '%s\n' "${targets[@]}" >> "$selection_dir/shared-targets"
        printf 'Combined lane %s retains AMD64 execution; no ARM64 coverage is added.\n' "$lane"
        options+=(--strip=never)
        ;;
    esac
  done
  # Explicit cgroup selections already applied each lane's filters.
  if [[ $include_nogo == true && $explicit_unit == false ]]; then
    if [[ $include_unit == true ]]; then
      options+=(--test_tag_filters=-requires-kvm)
    elif [[ $include_syscalls == true && $include_checkpoints == false ]]; then
      options+=('--test_tag_filters=-allsave,-runsc_kvm,-runsc_slimvm')
    fi
  fi
  if [[ -s $selection_dir/filtered-targets ]]; then
    # Select only these profiles under their KVM policy. Applying it to the final
    # invocation would also change unrelated lanes' filters.
    analyze_profile "$selection_dir/filtered-targets" "$selection_dir/filtered-profile.json" \
      --config=rbe-matrix --config=x86_64 --strip=never \
      --build_tests_only --test_tag_filters=-requires-kvm
    python3 test/rbe/unit_matrix.py kvm-query "$selection_dir/filtered-targets" \
      > "$selection_dir/filtered-kvm.query"
    bazel query --output=label --query_file="$selection_dir/filtered-kvm.query" \
      > "$selection_dir/filtered-kvm-targets"
    python3 test/rbe/unit_matrix.py select-filtered "$selection_dir/filtered-profile.json" \
      "$selection_dir/filtered-kvm-targets" "$selection_dir/filtered-selected-targets"
    cat "$selection_dir/filtered-selected-targets" >> "$selection_dir/explicit-targets"
  fi
  cat "$selection_dir/explicit-targets" "$selection_dir/shared-targets" > "$selection_dir/combined-targets"
  if [[ -s $selection_dir/combined-targets ]]; then
    if [[ $include_nogo == true || $include_syscalls == true || $include_public_profiles == true || $explicit_unit == true || -s $selection_dir/filtered-targets || ( $include_unit == true && -s $selection_dir/shared-targets ) ]]; then
      if [[ -s $selection_dir/shared-targets ]]; then
        # Let Bazel expand explicit suites, including their manual members,
        # before comparing their selection under the combined lane's filters.
        analyze_profile "$selection_dir/shared-targets" "$selection_dir/shared-profile.json" \
          --config=rbe-matrix --config=x86_64 --strip=never --build_tests_only
        verification_options+=(--profile "$selection_dir/shared-profile.json")
      fi
      : > "$selection_dir/verification-targets"
      if [[ $include_nogo == true ]]; then
        cat "$selection_dir/targets" >> "$selection_dir/verification-targets"
      fi
      cat "$selection_dir/combined-targets" >> "$selection_dir/verification-targets"
      # Verify the final filters with Bazel itself. Never silently lose another
      # lane's explicit tests when combining it with the unit configuration.
      analyze_profile "$selection_dir/verification-targets" "$selection_dir/combined-profile.json" \
        --config=rbe-matrix --config=x86_64 "${options[@]}" --build_tests_only
      python3 test/rbe/unit_matrix.py verify \
        "$selection_dir/explicit-targets" "$selection_dir/combined-profile.json" "${verification_options[@]}"
    fi
    cat "$selection_dir/combined-targets" >> "$selection_dir/targets"
  fi
  if [[ ! -s $selection_dir/targets ]]; then
    printf 'No requested tests have available remote workers; see the profile exclusions above.\n' >&2
    return 2
  fi
  bazel "$command" --config=rbe-matrix --config=x86_64 --keep_going \
    --incompatible_sandbox_hermetic_tmp=false --test_output=errors "${options[@]}" "${header_options[@]}" \
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
    presubmit-build)
      local build_config build_root phase_status status=0
      local -a build_configs=("$architecture_config")
      if [[ $arch == all ]]; then
        build_configs=(x86_64 aarch64)
      fi
      for build_config in "${build_configs[@]}"; do
        for build_root in //pkg/... //runsc/...; do
          # Match the two public presubmit commands: pkg inherits -nogo;
          # runsc replaces that filter with -network_plugins.
          options=()
          if [[ $build_root == //runsc/... ]]; then
            options=(--build_tag_filters=-network_plugins)
          fi
          phase_status=0
          bazel build --config=rbe "--config=$build_config" --keep_going \
            "${options[@]}" -- "$build_root" || phase_status=$?
          printf 'Presubmit build %s (%s) exited %d\n' "$build_root" "$build_config" "$phase_status"
          if (( phase_status != 0 )); then
            status=1
          fi
        done
      done
      return "$status"
      ;;
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
      targets=(//runsc:runsc-plugin-stack-build)
      ;;
    codeql)
      command=build
      targets=(//tools/codeql:all)
      ;;
    lint)
      if [[ $arch != amd64 ]]; then
        printf 'Source lint tools run on AMD64 workers.\n' >&2
        return 2
      fi
      shared_test_targets "$lane" "$arch"
      ;;
    lint-cc)
      if [[ $arch == arm64 ]]; then
        printf 'The public clang-tidy lane is declared for AMD64.\n' >&2
        return 2
      fi
      command=build
      options=(--config=lint-cc)
      ;;
    license-check)
      if [[ $arch != amd64 ]]; then
        printf 'Hosted source tools are qualified only on the AMD64 coordinator.\n' >&2
        return 2
      fi
      run_license_check
      return "$?"
      ;;
    license-headers)
      shared_test_targets "$lane" "$arch"
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
      if [[ $test_execution == local ]]; then
        run_hybrid_profile unit
        return
      fi
      # test/unit.targets also retains non-test build targets and the existing
      # exclusions. Keep its selection separate from Nogo's positive tag filter.
      options=(--config=unit)
      ;;
    container|container-v1)
      if [[ $arch != amd64 ]]; then
        printf 'The public container lanes are declared for AMD64.\n' >&2
        return 2
      fi
      run_platform_matrix "$lane"
      return
      ;;
    smoke)
      targets=(//:release_smoke_test)
      if [[ $test_execution == local ]]; then
        # The existing variant binds the release and TestRunner to this CPU,
        # independently of the preferred remote compilation platform.
        targets=("//:release_smoke_test_$arch")
      fi
      ;;
    smoke-race)
      targets=(//:release_smoke_race_test)
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
    docker-v1)
      shared_test_targets docker "$arch"
      ;;
    plugin-network)
      if [[ $arch != amd64 ]]; then
        printf 'The public plugin network test is declared for AMD64.\n' >&2
        return 2
      fi
      shared_test_targets "$lane" "$arch"
      ;;
    bwrap)
      shared_test_targets "$lane" "$arch"
      if [[ $test_execution == local ]]; then
        targets=("${targets[0]}_$arch")
        # Match make bwrap-tests: only the test process needs root.
        options+=(--run_under='sudo -n -E' --test_arg=-test.v)
      fi
      ;;
    do|docker|root|portforward|workflows|governance|overlay|swgso|hostnet|containerd|fsstress|packetimpact|iptables|nftables|packetdrill|podman|cpu-images|gpu-images|cos-metadata)
      if [[ $lane == "do" && $arch != amd64 ]]; then
        printf 'The public do smoke checks are declared for AMD64.\n' >&2
        return 2
      fi
      shared_test_targets "$lane" "$arch"
      ;;
    posture|startup)
      shared_test_targets "$lane" "$arch"
      if [[ $test_execution == remote ]]; then
        options=(--test_tag_filters=-requires-kvm)
      fi
      ;;
    benchmarks)
      if [[ $arch != amd64 ]]; then
        printf 'Continuous CI benchmarks are declared for AMD64; ARM64 workers remain unqualified.\n' >&2
        return 2
      fi
      # CI reports these jobs as soft failures. Keep their status visible here;
      # --keep_going still collects the other complete benchmark workloads.
      if [[ $test_execution == remote ]]; then
        options=(--test_tag_filters=-requires-kvm)
      fi
      shared_test_targets "$lane" "$arch"
      if [[ -n $benchmark_target ]]; then
        local selection_dir=${RUNNER_TEMP:?}/qualification/benchmarks-selection
        mkdir -p "$selection_dir"
        # Query the owning suite rather than maintaining a second target list.
        bazel query 'tests(//test/benchmarks:continuous_tests)' --output=label \
          > "$selection_dir/canonical-targets"
        if ! grep -Fxq -- "$benchmark_target" "$selection_dir/canonical-targets"; then
          printf 'Not a continuous benchmark target: %s\n' "$benchmark_target" >&2
          return 2
        fi
        printf '%s\n' "$benchmark_target" > "$selection_dir/selected-targets"
        targets=("$benchmark_target")
      fi
      ;;
    language-directfs|language-goferfs)
      if [[ $arch != amd64 ]]; then
        printf 'Language runtime images are declared only for AMD64.\n' >&2
        return 1
      fi
      language_test_options
      shared_test_targets "$lane" "$arch"
      ;;
    kubernetes)
      if [[ $arch != amd64 ]]; then
        printf 'The kind tool and node image are declared only for AMD64.\n' >&2
        return 1
      fi
      shared_test_targets "$lane" "$arch"
      ;;
    syzkaller)
      if [[ $arch != amd64 ]]; then
        printf 'The Syzkaller smoke test is declared only for AMD64.\n' >&2
        return 2
      fi
      shared_test_targets "$lane" "$arch"
      ;;
    go-export)
      if [[ $arch != amd64 ]]; then
        printf 'The exported-module matrix runs on AMD64 workers.\n' >&2
        return 2
      fi
      shared_test_targets "$lane" "$arch"
      ;;
    website)
      if [[ $arch != amd64 ]]; then
        printf 'The public website lane is qualified only for AMD64.\n' >&2
        return 2
      fi
      command=build
      targets=(//website:image)
      ;;
    syscalls|syscalls-kvm|syscalls-save|syscalls-resume)
      if [[ $test_execution == local ]]; then
        run_hybrid_profile "$lane"
        return
      fi
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
    if [[ $test_execution == local ]]; then
      case "$lane" in
        startup|posture|portforward|root|benchmarks|docker|cpu-images|gpu-images)
          # Each owned daemon needs separate firewall state. The fixture
          # can attach this private namespace to the job's bridge.
          docker_test_options
          options+=(--strategy=TestRunner=docker --run_under=//test/rbe:docker_setup)
          if [[ $lane == benchmarks || $lane == docker || $lane == cpu-images || $lane == gpu-images ]]; then
            options+=(
              --sandbox_add_mount_pair=/var/run/docker.sock:/run/gvisor-host-docker.sock
              "--test_env=GVISOR_DOCKER_NETWORK=${GVISOR_DOCKER_NETWORK:?Run local Docker tests through test/rbe/actions.sh}"
              "--test_env=GVISOR_HOST_NET_NS=$(readlink /proc/self/ns/net)"
            )
          fi
          ;;
      esac
      options=(--config=rbe-local-tests "${options[@]}")
      if [[ ( $lane == docker || $lane == cpu-images || $lane == gpu-images ) && $arch == arm64 ]]; then
        # The unmodified suite needs native test wrappers and run_under tools.
        # Prefer ARM remote tools while Docker executes the tests on this host.
        execution_config=rbe-arm64
      fi
    elif [[ $arch == arm64 ]]; then
      execution_config=rbe-arm64
      printf 'ARM64 Firecracker capacity remains unqualified; namespace-dependent tests require it.\n'
    fi
  fi
  bazel "$command" "--config=$execution_config" "--config=$architecture_config" \
    --keep_going "${options[@]}" "${header_options[@]}" "${targets[@]}"
)

run_selection() {
  local status=0 lane lane_status
  local -a matrix_lanes=()
  for lane in "$@"; do
    # Recursive builds keep their own loading filters and output groups. A test
    # invocation would also execute unrelated tests below those package roots.
    if [[ $arch == all && $lane != presubmit-build && $lane != lint-cc ]]; then
      matrix_lanes+=("$lane")
      continue
    fi
    printf '\nRunning lane: %s\n' "$lane"
    run_lane "$lane"
    lane_status=$?
    printf 'Lane %s exited %d\n' "$lane" "$lane_status"
    if (( lane_status != 0 )); then
      status=1
    fi
  done
  if (( ${#matrix_lanes[@]} )); then
    run_platform_matrix "${matrix_lanes[@]}"
    lane_status=$?
    if (( lane_status != 0 )); then
      status=1
    fi
  fi
  return "$status"
}

for lane in "$@"; do
  if [[ $lane == cos-metadata ]]; then
    # Prepare the declared input before any selected lane runs, including the
    # analyses that establish a combined test selection.
    source tools/gpu/cos_metadata_input.sh
    with_cos_metadata_input "${COS_IMAGES_JSON:-}" run_selection "$@"
    exit "$?"
  fi
done
run_selection "$@"
