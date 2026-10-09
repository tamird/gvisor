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

lanes=(build-all presubmit-build plugin-build nogo unit unit-v1 container container-v1 smoke smoke-race release-artifacts release-repository cpu-images gpu-images cos-metadata docker docker-v1 overlay swgso hostnet plugin-network 'do' root portforward posture startup benchmarks containerd bwrap fsstress packetimpact iptables nftables moby kvm packetdrill language-directfs language-goferfs kubernetes podman syzkaller website go-export codeql workflows lint lint-cc governance license-check license-headers python-distributions syscalls syscalls-kvm syscalls-rc-pilot syscalls-64k syscalls-rc syscalls-save syscalls-resume)

usage() {
  cat <<'USAGE'
Usage: test/rbe/qualify.sh --header-base=REV amd64
       test/rbe/qualify.sh [--arch=amd64|arm64|all] [--test-execution=remote|local] [--syscall-bucket=0..14] [--benchmark-target=LABEL] [--header-base=REV] LANE [LANE ...]
       test/rbe/qualify.sh --list

Run Linux remote lanes using the configured Bazel RBE connection. The default
target architecture is AMD64. Tests use matching execution workers, except
syscalls-64k and ARM64 syscalls-rc, whose payloads use QEMU TCG on AMD64 OCI
workers. AMD64 RC guests run locally with nested KVM. Builds prefer AMD64 workers
while retaining declared native generator requirements.
This is partial public CI coverage;
selecting an architecture does not guarantee worker support. Existing failures
remain errors.
The all architecture selection combines unit, release-repository and syscalls
with the target-configured test lanes described in test/rbe/README.md.
Presubmit builds run separately for each requested CPU in the same job.
Clang-tidy retains a separate AMD64 aspect build over its recursive roots.
ARM64 selection follows the public unit, syscall, smoke, Docker, bwrap and
image-source lanes; unavailable workers are reported before execution.
The remote syscalls-64k lane selects the public ARM64 64K systrap profile.
The local AMD64 syscalls-rc-pilot lane runs four mincore owners in a pinned
RC guest, including nested KVM. It does not select the full RC profile.
The syscalls-rc lane selects the public ordinary profile for each requested CPU.
Use remote execution for ARM64, or local AMD64 execution for AMD64 or both CPUs.
The license-headers lane requires an explicit base and complete Git history.
The cos-metadata lane requires COS_IMAGES_JSON with the complete gcloud catalog.
Local execution supports smoke, bwrap, ordinary syscalls, ARM64 unit/resume tests
and AMD64 KVM package/integration tests, KVM/RC syscalls, nftables, Moby, PHP,
plugin-network and startup/posture/portforward/root/benchmarks.
With --arch=all, the unit lane uses an ARM64 coordinator and runs AMD64 and
ordinary ARM64 tests remotely, with ARM64 namespace tests on the coordinator.
Compilation remains remote. Hybrid profiles run in one invocation; each
test uses the execution requirements recorded in its selection report.
An optional syscall bucket selects one existing hash15 partition, not the full
profile. Its report retains every unexecuted bucket owner.
A benchmark target selects one member of the continuous suite, retaining its
original workload and timeout. Other suite members remain unexecuted.
USAGE
  printf '\nLanes: %s\n' "${lanes[*]}"
}

gaps() {
  cat <<'GAPS'
Environment limits: remote execution does not supply KVM, slimvm, ARM64
Firecracker, arbitrary alternate kernels, or GPU/TPU runtime environments.
The syscalls-64k and syscalls-rc routes supply pinned ARM64 kernels through QEMU TCG.
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
      all:aarch64)
        if [[ $# != 1 || $1 != unit ]]; then
          printf 'Mixed local execution on ARM64 requires the unit profile.\n' >&2
          exit 2
        fi
        ;;
      all:x86_64)
        if [[ $# != 1 || $1 != syscalls-rc ]]; then
          printf 'Mixed local execution is supported only for the RC guest profile.\n' >&2
          exit 2
        fi
        ;;
      *) printf 'Local tests require a single matching host architecture.\n' >&2; exit 2 ;;
    esac
    if (( $# != 1 )); then
      printf 'Select one lane for local tests.\n' >&2
      exit 2
    fi
    case "$1:$arch" in
      smoke:amd64|smoke:arm64|bwrap:amd64|bwrap:arm64|unit:arm64|unit:all|docker:arm64|cpu-images:arm64|gpu-images:arm64|syscalls:amd64|syscalls:arm64|syscalls-resume:arm64|syscalls-kvm:amd64|syscalls-rc-pilot:amd64|syscalls-rc:amd64|syscalls-rc:all|plugin-network:amd64|nftables:amd64|moby:amd64|kvm:amd64|language-goferfs:amd64|startup:amd64|posture:amd64|portforward:amd64|root:amd64|benchmarks:amd64) ;;
      *) printf 'Local tests support smoke, bwrap, ordinary syscalls, ARM64 unit/resume/Docker/image profiles and AMD64 KVM package/integration/syscall tests, nftables/Moby/PHP/plugin-network/startup/posture/portforward/root/benchmarks.\n' >&2; exit 2 ;;
    esac
    ;;
  *) printf 'Unknown test execution: %s\n' "$test_execution" >&2; exit 2 ;;
esac
if [[ -n $syscall_bucket ]]; then
  if [[ ! $syscall_bucket =~ ^([0-9]|1[0-4])$ || $# != 1 || ( $test_execution:$arch:${1:-} != local:arm64:syscalls && $test_execution:$arch:${1:-} != local:arm64:syscalls-resume && $test_execution:$arch:${1:-} != local:amd64:syscalls && $test_execution:$arch:${1:-} != local:amd64:syscalls-kvm && $test_execution:$arch:${1:-} != remote:arm64:syscalls-64k && $test_execution:$arch:${1:-} != remote:arm64:syscalls-rc && $test_execution:$arch:${1:-} != local:amd64:syscalls-rc && $test_execution:$arch:${1:-} != local:all:syscalls-rc ) ]]; then
    printf 'A syscall bucket must be 0..14 and requires a supported local or guest syscall profile.\n' >&2
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
    # Host-dependent and alternate-kernel lanes are selected explicitly.
    if [[ $lane != kvm && $lane != syscalls-kvm && $lane != moby && $lane != syscalls-rc-pilot && $lane != syscalls-64k && $lane != syscalls-rc ]]; then set -- "$@" "$lane"; fi
  done
fi
if (( $# == 0 )); then
  usage >&2
  exit 2
fi
# Validate every requested lane before starting any work.
for lane in "$@"; do
  if [[ $lane == moby && ( $test_execution != local || $arch != amd64 ) ]]; then
    printf 'The public Moby lane requires local AMD64 execution with the cgroup-v2 Docker fixture.\n' >&2
    exit 2
  fi
  if [[ $lane == syscalls-64k && ( $test_execution != remote || $arch != arm64 ) ]]; then
    printf 'The 64K syscall lane requires --arch=arm64 and remote TCG execution.\n' >&2
    exit 2
  fi
  if [[ $lane == syscalls-rc && $test_execution:$arch != remote:arm64 && $test_execution:$arch != local:amd64 && $test_execution:$arch != local:all ]]; then
    printf 'RC guests require remote ARM64 or local AMD64/all execution.\n' >&2
    exit 2
  fi
  if [[ ( $lane == kvm || $lane == syscalls-kvm || $lane == syscalls-rc-pilot ) && ( $test_execution != local || $arch != amd64 ) ]]; then
    printf 'The KVM syscall lane requires local AMD64 execution.\n' >&2
    exit 2
  fi
  if [[ $arch == all ]]; then
    case "$lane" in
      presubmit-build|nogo|unit|unit-v1|container|container-v1|docker-v1|release-artifacts|release-repository|python-distributions|website|syscalls|syscalls-rc|syscalls-save|syscalls-resume|smoke|smoke-race|plugin-build|plugin-network|do|docker|root|portforward|bwrap|workflows|lint|language-directfs|language-goferfs|overlay|swgso|hostnet|containerd|fsstress|packetimpact|iptables|nftables|packetdrill|kubernetes|podman|syzkaller|go-export|codeql|cpu-images|gpu-images|cos-metadata|posture|startup|benchmarks|governance|license-headers|lint-cc) ;;
      *) printf 'Lane %s does not support the all architecture selection.\n' "$lane" >&2; exit 2 ;;
    esac
  fi
  case "$lane" in
    build-all|presubmit-build|plugin-build|nogo|unit|unit-v1|container|container-v1|smoke|smoke-race|release-artifacts|release-repository|cpu-images|gpu-images|cos-metadata|docker|docker-v1|overlay|swgso|hostnet|plugin-network|do|root|portforward|posture|startup|benchmarks|containerd|bwrap|fsstress|packetimpact|iptables|nftables|moby|kvm|packetdrill|language-directfs|language-goferfs|kubernetes|podman|syzkaller|website|go-export|codeql|workflows|lint|lint-cc|governance|license-check|license-headers|python-distributions|syscalls|syscalls-kvm|syscalls-rc-pilot|syscalls-64k|syscalls-rc|syscalls-save|syscalls-resume) ;;
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
    moby) targets=(//test/moby:moby_owned) ;;
    kvm) targets=(//pkg/sentry/platform/kvm:kvm_test //test/docker:kvm_tests) ;;
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
  if [[ $lane == syscalls-64k || $lane == syscalls-rc ]]; then
    if [[ $lane == syscalls-64k ]]; then
      page_size_options=(--page-size=64k)
    else
      page_size_options=(--rc-kernel)
    fi
    routing_options=(--//tools/bazeldefs:local_test_architecture= --//tools/bazeldefs:page_size=4k)
    if [[ -n $syscall_bucket ]]; then
      selection_options+=("--syscall-bucket=$syscall_bucket")
    fi
  fi
  if [[ $test_execution == local && $lane != syscalls-rc ]]; then
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
    syscalls-rc) profile_options=("--config=syscalls-$syscall_arch" --//tools/bazeldefs:page_size=4k) ;;
    syscalls-save) profile_options=(--test_tag_filters=save_restore) ;;
    syscalls-resume) profile_options=(--test_tag_filters=save_resume) ;;
    *) printf 'Unknown syscall profile: %s\n' "$lane" >&2; return 2 ;;
  esac
  select_test_profile "$selection_dir" "$lane" "$syscall_arch" \
    test/syscalls.targets "${profile_options[@]}"
}

# Each selected owner retains its own guest and original test deadline. ARM64
# uses remote TCG; AMD64 uses local nested KVM. Both payloads compile remotely.
run_guest_profile() (
  set -e
  local lane=$1 selection_dir target_arch
  local -a architectures=("$arch") options=()
  if [[ $arch == all ]]; then
    architectures=(amd64 arm64)
  fi
  selection_dir=$(mktemp -d)
  trap 'rm -rf "$selection_dir"' EXIT
  for target_arch in "${architectures[@]}"; do
    select_syscall_profile "$selection_dir" "$lane" "$target_arch" | tee "$selection_dir/$target_arch-selection.json"
    cat "$selection_dir/$lane-$target_arch-targets" >> "$selection_dir/targets"
  done
  if [[ $arch == all ]]; then
    # Reconcile the combined configuration with both independently selected
    # public profiles before dispatching any TestRunner.
    python3 test/rbe/unit_matrix.py actions "$selection_dir/targets" --exact > "$selection_dir/combined.query"
    bazel aquery --config=rbe-matrix --config=x86_64 --build_tests_only \
      --//tools/bazeldefs:local_test_architecture= --//tools/bazeldefs:page_size=4k \
      --output=jsonproto --include_artifacts=false \
      "--build_event_json_file=$selection_dir/profile.json" \
      "--query_file=$selection_dir/combined.query" > "$selection_dir/actions.json"
    python3 test/rbe/unit_matrix.py verify "$selection_dir/targets" "$selection_dir/profile.json"
    python3 - "$selection_dir" <<'PY'
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
profiles = {arch: json.loads((root / f"{arch}-selection.json").read_text()) for arch in ("amd64", "arm64")}
selected = [label for profile in profiles.values() for label in profile["selected_owners"]]
assert len(selected) == len(set(selected)), "RC architecture selections overlap"
(root / "selection.json").write_text(json.dumps({"profile_architecture": "all", "profile_kernel": "rc", "profiles": profiles, "selected_owners": sorted(selected)}, indent=2) + "\n")
PY
  else
    cp "$selection_dir/$arch-selection.json" "$selection_dir/selection.json"
    cp "$selection_dir/$lane-$arch-actions.json" "$selection_dir/actions.json"
    cp "$selection_dir/$lane-$arch-profile.json" "$selection_dir/profile.json"
  fi
  if [[ -n ${RUNNER_TEMP:-} ]]; then
    save_profile_selection "$selection_dir" "$lane"
  fi
  if [[ $test_execution == local ]]; then
    options=(--config=rbe-hybrid-tests --local_test_jobs=1)
  fi
  bazel test --config=rbe --config=x86_64 --keep_going \
    --//tools/bazeldefs:local_test_architecture= --//tools/bazeldefs:page_size=4k \
    --strip=never --incompatible_sandbox_hermetic_tmp=false --test_output=errors \
    "${options[@]}" --target_pattern_file="$selection_dir/targets"
)

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

save_profile_selection() {
  local selection_dir=$1 lane=$2
  # Preserve the selection, but never upload Bazel's parsed credential options.
  mkdir -p "${RUNNER_TEMP:?}/qualification/$lane-selection"
  cp "$selection_dir/selection.json" "$selection_dir/actions.json" \
    "$selection_dir/targets" "$RUNNER_TEMP/qualification/$lane-selection/"
  if [[ -f $selection_dir/combined-actions.json ]]; then
    cp "$selection_dir/combined-actions.json" "$RUNNER_TEMP/qualification/$lane-selection/"
    # Read the heap limit from the same server that analyzed the mixed graph.
    bazel info max-heap-size > "$RUNNER_TEMP/qualification/$lane-selection/max-heap-size.txt"
  fi
  if [[ -f $selection_dir/owners ]]; then
    cp "$selection_dir/owners" "$RUNNER_TEMP/qualification/$lane-selection/"
  fi
  python3 - "$selection_dir" "$RUNNER_TEMP/qualification/$lane-selection" <<'PY'
import json
from pathlib import Path
import sys

keys = {"id", "children", "configured", "finished", "aborted"}
for source in Path(sys.argv[1]).glob("*.json"):
    if source.name == "profile.json" or source.name.endswith(("-profile.json", "-routing.json")):
        with (Path(sys.argv[2]) / source.name).open("w") as output:
            for line in source.read_text().splitlines():
                event = json.loads(line)
                output.write(json.dumps({key: value for key, value in event.items() if key in keys}) + "\n")
PY
}

# Reuse the native graph and canonical profile in one invocation. The
# frontend owns each test's local namespace and execution requirements.
run_hybrid_profile() (
  set -e
  local lane=$1 selection_dir local_arch=$arch
  local -a lane_options=() options=() selection_options=()
  selection_dir=$(mktemp -d)
  local artifacts="$RUNNER_TEMP/qualification/tun-restore-current"
  mkdir -p "$artifacts"
  tun_cleanup() {
    local status=$?
    trap '' TERM INT
    trap - EXIT
    local file
    for file in pkg/tcpip/link/tun/device.go pkg/tcpip/stack/stack.go; do
      if [[ -f $selection_dir/after/$file ]]; then
        cp "$selection_dir/after/$file" "$file" || status=1
        cmp "$selection_dir/after/$file" "$file" || status=1
      fi
    done
    git diff --exit-code -- pkg/tcpip/link/tun/device.go pkg/tcpip/stack/stack.go || status=1
    printf '%s\n' "$status" > "$artifacts/cleanup-exit-code"
    rm -rf "$selection_dir"
    exit "$status"
  }
  trap tun_cleanup EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT
  if [[ $lane != syscalls || $arch != amd64 || -n $syscall_bucket ]]; then
    printf 'This diagnostic requires the existing AMD64 syscall route.\n' >&2
    exit 2
  fi
  if [[ $lane == unit ]]; then
    local_arch=arm64
    python3 test/rbe/unit_matrix.py query test/unit.targets > "$selection_dir/owners.query"
    bazel query --output=label --query_file="$selection_dir/owners.query" > "$selection_dir/owners"
    python3 test/rbe/unit_matrix.py actions "$selection_dir/owners" > "$selection_dir/actions.query"
    bazel aquery --config=rbe-matrix --config=x86_64 \
      --//tools/bazeldefs:local_test_architecture=arm64 --output=jsonproto --include_artifacts=false \
      --query_file="$selection_dir/actions.query" > "$selection_dir/actions.json"
    analyze_profile test/unit.targets "$selection_dir/profile.json" \
      --config=rbe-matrix --config=aarch64 --config=unit --strip=never --build_tests_only
    if [[ $arch == all ]]; then
      analyze_profile test/unit.targets "$selection_dir/amd64-profile.json" \
        --config=rbe-matrix --config=x86_64 --config=unit --strip=never --build_tests_only
      selection_options+=(--amd64-profile "$selection_dir/amd64-profile.json")
      lane_options+=(--config=unit)
    fi
    python3 test/rbe/unit_matrix.py select test/unit.targets "$selection_dir/owners" \
      "$selection_dir/actions.json" "$selection_dir/targets" --profile "$selection_dir/profile.json" --hybrid "${selection_options[@]}" \
      | tee "$selection_dir/selection.json"
  else
    lane_options=(--cxxopt=-Werror)
    select_syscall_profile "$selection_dir" "$lane" "$arch" | tee "$selection_dir/selection.json"
    cp "$selection_dir/$lane-$arch-targets" "$selection_dir/targets"
    cp "$selection_dir/$lane-$arch-actions.json" "$selection_dir/actions.json"
    cp "$selection_dir/$lane-$arch-profile.json" "$selection_dir/profile.json"
  fi
  if [[ $lane == unit && $arch == all ]]; then
    # Capture both configured architectures, including each original shard.
    python3 - "$selection_dir/selection.json" "$selection_dir/combined-owners" <<'PYOWNERS'
import json
from pathlib import Path
import sys

selected = json.loads(Path(sys.argv[1]).read_text())["selected_owners"]
Path(sys.argv[2]).write_text("".join(label + "\n" for label in selected))
PYOWNERS
    python3 test/rbe/unit_matrix.py actions "$selection_dir/combined-owners" --exact \
      > "$selection_dir/combined-actions.query"
    bazel aquery --config=rbe --config=x86_64 --config=rbe-hybrid-tests --config=unit --strip=never \
      --//tools/bazeldefs:local_test_architecture=arm64 \
      --incompatible_sandbox_hermetic_tmp=false --test_env=GO_TEST_WRAP_TESTV=1 \
      --output=jsonproto --include_artifacts=false \
      --query_file="$selection_dir/combined-actions.query" > "$selection_dir/combined-actions.json"
    # Reuse valid test results from the interrupted complete-lane attempt.
    options+=(--cache_test_results=auto)
  fi
  save_profile_selection "$selection_dir" "$lane"
  printf '%s %s profile: test placement follows the recorded per-target requirements; compilation stays remote.\n' "$arch" "$lane"
  if [[ -n $syscall_bucket ]]; then
    printf 'Running syscall hash15 bucket %s only; the other buckets remain unexecuted.\n' "$syscall_bucket"
  fi
  if [[ $lane == syscalls && $arch == amd64 ]]; then
    docker_test_options
    options+=("--strategy=TestRunner=remote,docker,local" --//tools/bazeldefs:local_test_backend=docker)
  fi

  python3 - "$selection_dir" "$artifacts" <<'PYTUN_SELECTION'
import hashlib
import json
from pathlib import Path
import sys
selection_dir, artifacts = map(Path, sys.argv[1:])
oracle = json.loads('{"source":"072b93c00c68d34ce9a85f40290db9595627572b","beforeSource":"939e5985c8d36ef6171b206f5555266f8284e7d1","owners":[{"label":"//test/syscalls:tuntap_test_native_amd64","shards":1,"timeout_seconds":300},{"label":"//test/syscalls:tuntap_test_runsc_systrap_directfs_save_amd64","shards":1,"timeout_seconds":900},{"label":"//test/syscalls:tuntap_test_runsc_systrap_shared_save_amd64","shards":1,"timeout_seconds":900},{"label":"//pkg/tcpip/stack:stack_test","shards":1,"timeout_seconds":60},{"label":"//pkg/tcpip/stack:stack_x_test","shards":8,"timeout_seconds":60},{"label":"//pkg/sentry/socket/netstack:netstack_test","shards":1,"timeout_seconds":300},{"label":"//pkg/tcpip/stack:stack_test_nogo","shards":1,"timeout_seconds":300},{"label":"//pkg/tcpip/stack:stack_x_test_nogo","shards":1,"timeout_seconds":300},{"label":"//pkg/sentry/socket/netstack:netstack_test_nogo","shards":1,"timeout_seconds":300},{"label":"//runsc/container:container_test_nogo","shards":1,"timeout_seconds":300},{"label":"//pkg/tcpip/transport/tcp/test/e2e:tcp_test","shards":4,"timeout_seconds":900}],"phases":{"before":[{"label":"//test/syscalls:tuntap_test_runsc_systrap_directfs_save_amd64","shards":1,"timeout_seconds":900}],"after":[{"label":"//test/syscalls:tuntap_test_native_amd64","shards":1,"timeout_seconds":300},{"label":"//test/syscalls:tuntap_test_runsc_systrap_directfs_save_amd64","shards":1,"timeout_seconds":900},{"label":"//test/syscalls:tuntap_test_runsc_systrap_shared_save_amd64","shards":1,"timeout_seconds":900}],"units":[{"label":"//pkg/tcpip/stack:stack_test","shards":1,"timeout_seconds":60},{"label":"//pkg/tcpip/stack:stack_x_test","shards":8,"timeout_seconds":60},{"label":"//pkg/sentry/socket/netstack:netstack_test","shards":1,"timeout_seconds":300}],"nogo":[{"label":"//pkg/tcpip/stack:stack_test_nogo","shards":1,"timeout_seconds":300},{"label":"//pkg/tcpip/stack:stack_x_test_nogo","shards":1,"timeout_seconds":300},{"label":"//pkg/sentry/socket/netstack:netstack_test_nogo","shards":1,"timeout_seconds":300},{"label":"//runsc/container:container_test_nogo","shards":1,"timeout_seconds":300}],"migration":[{"label":"//pkg/tcpip/transport/tcp/test/e2e:tcp_test","shards":4,"timeout_seconds":900}]},"runtimeCase":"TuntapTest.SaveRestoreAfterNetnsMove","migrationCase":"TestSaveAfterRestoreWithReplaceConfig","sourceHashes":{"before":{"pkg/tcpip/link/tun/device.go":"4cfc7b79c8a94d7a2e0f464856ea0af8b5ac3282ce85222cc8dbc5fa2b0cf704","pkg/tcpip/stack/stack.go":"4e5a2ea154ce6f934c804be24338ea0ba8c84ede213bc41640bac6d7193e3d9c"},"after":{"pkg/tcpip/link/tun/device.go":"a74b70e5b71a75fac06e8242fa7e8cd27e96a3d12a72e98ea22a9e9a2e8c4d60","pkg/tcpip/stack/stack.go":"604807e21851eb31a18814d9e93e58d559dc1484768279fccaaeacefd9bc34b4"}}}')
selection = json.loads((selection_dir / "selection.json").read_text())
full = set(selection["selected_owners"])
assert "//test/syscalls:tuntap_test_native_amd64" in full
local = {label for labels in selection["local_owners"].values() for label in labels}
assert "//test/syscalls:tuntap_test_native_amd64" in local
assert not {row["label"] for row in oracle["owners"]}.intersection(selection.get("initial_cgroup_owners", []))
for phase, rows in oracle["phases"].items():
    (artifacts / (phase + "-targets")).write_text("".join(row["label"] + "\n" for row in rows))
(artifacts / "oracle.json").write_text(json.dumps(oracle, indent=2) + "\n")
(artifacts / "ordinary-selection-sha256.txt").write_text(hashlib.sha256((selection_dir / "selection.json").read_bytes()).hexdigest() + "\n")
(artifacts / "selected.query").write_text("set(" + " ".join(row["label"] for row in oracle["owners"]) + ")\n")
PYTUN_SELECTION
  bazel query --output=xml --xml:default_values --query_file="$artifacts/selected.query" > "$artifacts/declarations.xml"
  python3 - "$artifacts" <<'PYTUN_DECLARATIONS'
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
artifacts = Path(sys.argv[1])
oracle = json.loads((artifacts / "oracle.json").read_text())
rules = {r.attrib["name"]: r for r in ET.parse(artifacts / "declarations.xml").getroot().findall("rule")}
assert set(rules) == {row["label"] for row in oracle["owners"]}
timeouts = {"short": 60, "moderate": 300, "long": 900, "eternal": 3600}
for row in oracle["owners"]:
    rule = rules[row["label"]]
    values = {n.attrib["name"]: n.attrib.get("value") for n in rule if "name" in n.attrib}
    assert max(1, int(values["shard_count"])) == row["shards"], (row, values)
    assert timeouts[values["timeout"]] == row["timeout_seconds"], (row, values)
    if "_save_amd64" in row["label"]:
        tags = {n.attrib["value"] for group in rule.findall("list") if group.get("name") == "tags" for n in group}
        assert "save_restore" in tags, (row, tags)
(artifacts / "declarations-checked.json").write_text(json.dumps({"targets": len(rules), "testActions": sum(r["shards"] for rows in oracle["phases"].values() for r in rows)}) + "\n")
PYTUN_DECLARATIONS
  local file phase_status=0 aggregate_status=0
  for file in pkg/tcpip/link/tun/device.go pkg/tcpip/stack/stack.go; do
    mkdir -p "$selection_dir/after/$(dirname "$file")" "$artifacts/after-source/$(dirname "$file")"
    cp "$file" "$selection_dir/after/$file"
    cp "$file" "$artifacts/after-source/$file"
  done
  tun_phase() {
    local phase=$1 placement=$2
    shift 2
    python3 - "$artifacts" "$phase" <<'PYTUN_SOURCE'
import hashlib
import json
from pathlib import Path
import sys
artifacts, phase = Path(sys.argv[1]), sys.argv[2]
oracle = json.loads((artifacts / "oracle.json").read_text())
expected = oracle["sourceHashes"]["before" if phase == "before" else "after"]
actual = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in expected}
assert actual == expected, (phase, actual, expected)
(artifacts / (phase + "-source.json")).write_text(json.dumps(actual, indent=2) + "\n")
PYTUN_SOURCE
    local -a phase_options=()
    if [[ $placement == docker ]]; then
      phase_options=(--config=rbe-hybrid-tests "--//tools/bazeldefs:local_test_architecture=$local_arch" "${lane_options[@]}" "${options[@]}")
    else
      phase_options=(--strategy=TestRunner=remote --//tools/bazeldefs:local_test_architecture=)
    fi
    set +e
    bazel test --config=rbe --config=x86_64 --keep_going \
      --strip=never --incompatible_sandbox_hermetic_tmp=false --test_output=errors \
      --nocache_test_results --runs_per_test=1 --flaky_test_attempts=1 \
      --test_env=GO_TEST_WRAP_TESTV=1 "${phase_options[@]}" \
      "--build_metadata=TUN_RESTORE_PHASE=$phase" \
      "--build_event_json_file=$selection_dir/$phase-bep.json" \
      "$@" --target_pattern_file="$artifacts/$phase-targets"
    phase_status=$?
    set -e
    printf '%s\n' "$phase_status" > "$artifacts/$phase-exit-code"
    if (( phase_status != 0 )); then aggregate_status=1; fi
    local capture_status=0
    python3 - "$selection_dir/$phase-bep.json" "$artifacts/$phase-bep.json" <<'PYNATIVE_BEP' || capture_status=$?
import json
from pathlib import Path
import sys

source, target = map(Path, sys.argv[1:])
keys = {"id", "children", "started", "finished", "configured", "completed", "testResult", "testSummary", "aborted", "namedSetOfFiles"}
errors = []
with target.open("w") as output:
    if source.exists():
        for line_number, line in enumerate(source.read_text().splitlines(), 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                errors.append({"line": line_number, "error": str(error)})
                continue
            if "started" in event:
                safe = {"uuid", "startTime", "startTimeMillis", "command"}
                event["started"] = {key: value for key, value in event["started"].items() if key in safe}
            serialized = json.dumps({key: value for key, value in event.items() if key in keys})
            assert "x-buildbuddy-api-key" not in serialized.lower()
            output.write(serialized + "\n")
    else:
        errors.append({"error": "BEP absent"})
target.with_suffix(".errors.json").write_text(json.dumps(errors) + "\n")
raise SystemExit(bool(errors))
PYNATIVE_BEP
    printf '%s\n' "$capture_status" > "$artifacts/$phase-capture-exit-code"
    if (( capture_status != 0 )); then aggregate_status=1; fi
  }
  python3 - "$artifacts" <<'PYTUN_BEFORE'
import base64
import gzip
import hashlib
import json
from pathlib import Path
import sys
artifacts = Path(sys.argv[1])
oracle = json.loads((artifacts / "oracle.json").read_text())
encoded = {'pkg/tcpip/link/tun/device.go': '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', 'pkg/tcpip/stack/stack.go': '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'}
for name, value in encoded.items():
    path = Path(name)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == oracle["sourceHashes"]["after"][name]
    data = gzip.decompress(base64.b64decode(value, validate=True))
    assert hashlib.sha256(data).hexdigest() == oracle["sourceHashes"]["before"][name]
    path.write_bytes(data)
    capture = artifacts / "before-source" / name
    capture.parent.mkdir(parents=True, exist_ok=True)
    capture.write_bytes(data)
PYTUN_BEFORE
  git diff -- pkg/tcpip/link/tun/device.go pkg/tcpip/stack/stack.go > "$artifacts/before-source.patch"
  tun_phase before docker --test_filter=TuntapTest.SaveRestoreAfterNetnsMove
  for file in pkg/tcpip/link/tun/device.go pkg/tcpip/stack/stack.go; do
    cp "$selection_dir/after/$file" "$file"
    cmp "$selection_dir/after/$file" "$file"
  done
  git diff --exit-code -- pkg/tcpip/link/tun/device.go pkg/tcpip/stack/stack.go
  tun_phase after docker --test_filter=TuntapTest.SaveRestoreAfterNetnsMove
  tun_phase units remote
  tun_phase nogo remote
  tun_phase migration remote --test_filter=^TestSaveAfterRestoreWithReplaceConfig$
  git diff --exit-code -- pkg/tcpip/link/tun/device.go pkg/tcpip/stack/stack.go
  printf '%s\n' "$aggregate_status" > "$artifacts/aggregate-exit-code"
  return "$aggregate_status"
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
    do|docker|root|portforward|workflows|governance|overlay|swgso|hostnet|containerd|fsstress|packetimpact|iptables|nftables|moby|kvm|packetdrill|podman|cpu-images|gpu-images|cos-metadata)
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
      if [[ $test_execution == local ]]; then
        # This fork qualification covers both public PHP filesystem modes in
        # one invocation, sharing the current source-built image. Preserve each
        # mode's two public partitions and the owning rules' four shards.
        local selection_dir=${RUNNER_TEMP:?}/qualification/php-selection
        mkdir -p "$selection_dir" || return
        bazel query 'tests(//test/runtimes:php8.5.11_directfs_owned) union tests(//test/runtimes:php8.5.11_goferfs_owned)' \
          --output=label > "$selection_dir/canonical-targets" || return
        LC_ALL=C sort "$selection_dir/canonical-targets" > "$selection_dir/selected-targets" || return
        printf '%s\n' \
          '//test/runtimes:php8.5.11_directfs_1_owned' \
          '//test/runtimes:php8.5.11_directfs_2_owned' \
          '//test/runtimes:php8.5.11_goferfs_1_owned' \
          '//test/runtimes:php8.5.11_goferfs_2_owned' \
          > "$selection_dir/expected-targets" || return
        diff -u "$selection_dir/expected-targets" "$selection_dir/selected-targets" || return
        mapfile -t targets < "$selection_dir/selected-targets"
        options+=(--runs_per_test=1)
      fi
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
    syscalls-rc-pilot)
      # Preserve original mincore arguments, shards and deadlines. The guest
      # must exercise runsc's KVM platform, not merely expose /dev/kvm.
      targets=(
        //test/syscalls:mincore_test_native_rc_kvm
        //test/syscalls:mincore_test_runsc_ptrace_rc_kvm
        //test/syscalls:mincore_test_runsc_systrap_shared_rc_kvm
        //test/syscalls:mincore_test_runsc_kvm_rc_kvm
      )
      options=(--//tools/bazeldefs:page_size=4k --//tools/bazeldefs:local_test_architecture=)
      ;;
    syscalls-64k|syscalls-rc)
      run_guest_profile "$lane"
      return
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
        plugin-network|nftables|moby|kvm|language-goferfs|startup|posture|portforward|root|benchmarks|docker|cpu-images|gpu-images)
          # Each owned daemon needs separate firewall state. The fixture
          # can attach this private namespace to the job's bridge.
          docker_test_options
          if [[ $lane == language-goferfs ]]; then
            # Keep CPU-heavy PHP shards from competing on the same host.
            options+=(--local_test_jobs=1)
          fi
          options+=(--strategy=TestRunner=docker --run_under=//test/rbe:docker_setup)
          if [[ $lane == language-goferfs || $lane == moby || $lane == kvm || $lane == benchmarks || $lane == docker || $lane == cpu-images || $lane == gpu-images ]]; then
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
    if [[ $arch == all && $test_execution == remote && $lane != presubmit-build && $lane != lint-cc && $lane != syscalls-rc ]]; then
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
