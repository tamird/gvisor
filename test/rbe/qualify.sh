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
  local artifacts="$RUNNER_TEMP/qualification/timer-rlimit"
  mkdir -p "$artifacts"
  timer_cleanup() {
    local status=$?
    trap '' TERM INT
    trap - EXIT
    if [[ -f $selection_dir/timers.after.cc ]]; then
      cp "$selection_dir/timers.after.cc" test/syscalls/linux/timers.cc || status=1
      cmp "$selection_dir/timers.after.cc" test/syscalls/linux/timers.cc || status=1
    fi
    git diff --exit-code -- test/syscalls/linux/timers.cc || status=1
    printf '%s\n' "$status" > "$artifacts/cleanup-exit-code"
    rm -rf "$selection_dir"
    exit "$status"
  }
  trap timer_cleanup EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT
  if [[ $lane != syscalls || $arch != amd64 || -n $syscall_bucket ]]; then
    printf 'This diagnostic requires the complete AMD64 syscall graph.\n' >&2
    exit 2
  fi
  # Read-only host accounting facts; missing configuration remains unknown.
  {
    printf 'user_hz=%s\n' "$(getconf CLK_TCK)"
    if [[ -r /boot/config-$(uname -r) ]]; then
      grep -E '^CONFIG_(HZ|HZ_[0-9]+|NO_HZ|NO_HZ_FULL|NO_HZ_IDLE|TICK_CPU_ACCOUNTING|VIRT_CPU_ACCOUNTING.*|IRQ_TIME_ACCOUNTING|CONTEXT_TRACKING.*)=' "/boot/config-$(uname -r)" || true
    elif [[ -r /proc/config.gz ]]; then
      zcat /proc/config.gz | grep -E '^CONFIG_(HZ|HZ_[0-9]+|NO_HZ|NO_HZ_FULL|NO_HZ_IDLE|TICK_CPU_ACCOUNTING|VIRT_CPU_ACCOUNTING.*|IRQ_TIME_ACCOUNTING|CONTEXT_TRACKING.*)=' || true
    else
      printf 'kernel_accounting_configuration=unavailable\n'
    fi
  } > "$artifacts/host-accounting.txt"
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
  options+=(--local_test_jobs=1)
  python3 - "$selection_dir" "$artifacts" <<'PYTIMER_SELECTION'
import hashlib
import json
from pathlib import Path
import sys
selection_dir, artifacts = map(Path, sys.argv[1:])
oracle = json.loads('{"full_expected_owner_count":1176,"full_expected_owner_sha256":"34cd148e472a97ace77d75c747e195c3bebee52310e0e5766a3953fd3f969222","owners":[{"label":"//test/syscalls:timers_test_native_amd64","shards":1,"timeout_seconds":300},{"label":"//test/syscalls:timers_test_runsc_ptrace_amd64","shards":1,"timeout_seconds":300},{"label":"//test/syscalls:timers_test_runsc_systrap_directfs_amd64","shards":1,"timeout_seconds":300},{"label":"//test/syscalls:timers_test_runsc_systrap_shared_amd64","shards":1,"timeout_seconds":300}],"before_filter":"TimerTest.RlimitCpuInheritedAcrossFork","before_source_sha256":"99e87c3cf3973545281cb476062975d990cb1287f28855a297b52284d1c77409","after_source_sha256":"0c37a60fccf01a5c5df336568ca4b3b843587352bc3d3ff1c0ef64efccec52c8"}')
selection = json.loads((selection_dir / "selection.json").read_text())
full = sorted(selection["selected_owners"])
assert len(full) == oracle["full_expected_owner_count"]
assert hashlib.sha256(("\n".join(full) + "\n").encode()).hexdigest() == oracle["full_expected_owner_sha256"]
expected = {row["label"] for row in oracle["owners"]}
assert len(expected) == 4 and expected.issubset(full)
local = {label for labels in selection["local_owners"].values() for label in labels}
assert expected.issubset(local)
assert not expected.intersection(selection.get("initial_cgroup_owners", []))
(artifacts / "before-targets").write_text("//test/syscalls:timers_test_native_amd64\n")
(artifacts / "after-targets").write_text("".join(label + "\n" for label in sorted(expected)))
(selection_dir / "targets").write_text((artifacts / "after-targets").read_text())
(artifacts / "selection.json").write_text(json.dumps({"oracle": oracle, "unexecuted_owners": sorted(set(full) - expected), "full_selection_sha256": hashlib.sha256((selection_dir / "selection.json").read_bytes()).hexdigest()}, indent=2) + "\n")
(artifacts / "selected.query").write_text("set(" + " ".join(sorted(expected)) + ")\n")
PYTIMER_SELECTION
  bazel query --output=xml --xml:default_values --query_file="$artifacts/selected.query" > "$artifacts/declarations.xml"
  python3 - "$artifacts" <<'PYTIMER_DECLARATIONS'
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
artifacts = Path(sys.argv[1])
rows = json.loads((artifacts / "selection.json").read_text())["oracle"]["owners"]
rules = {rule.attrib["name"]: rule for rule in ET.parse(artifacts / "declarations.xml").getroot().findall("rule")}
assert set(rules) == {row["label"] for row in rows}
timeouts = {"short": 60, "moderate": 300, "long": 900, "eternal": 3600}
for row in rows:
    values = {node.attrib["name"]: node.attrib.get("value") for node in rules[row["label"]] if "name" in node.attrib}
    assert max(1, int(values["shard_count"])) == row["shards"], (row, values)
    assert timeouts[values["timeout"]] == row["timeout_seconds"], (row, values)
(artifacts / "declarations-checked.json").write_text(json.dumps({"targets": len(rows), "shards": sum(row["shards"] for row in rows)}) + "\n")
PYTIMER_DECLARATIONS
  cp test/syscalls/linux/timers.cc "$selection_dir/timers.after.cc"
  cp "$selection_dir/timers.after.cc" "$artifacts/timers.after.cc"
  local phase_status=0 aggregate_status=0
  timer_phase() {
    local phase=$1 phase_targets=$2
    shift 2
    python3 - "$artifacts" "$phase" <<'PYTIMER_SOURCE'
import hashlib
import json
from pathlib import Path
import sys
artifacts, phase = Path(sys.argv[1]), sys.argv[2]
data = Path("test/syscalls/linux/timers.cc").read_bytes()
oracle = json.loads((artifacts / "selection.json").read_text())["oracle"]
actual = hashlib.sha256(data).hexdigest()
assert actual == oracle[phase + "_source_sha256"], (phase, actual)
(artifacts / (phase + "-source.json")).write_text(json.dumps({"phase": phase, "timers_source_sha256": actual}) + "\n")
PYTIMER_SOURCE
    set +e
    bazel test --config=rbe --config=x86_64 --config=rbe-hybrid-tests --keep_going \
      "--//tools/bazeldefs:local_test_architecture=$local_arch" \
      --strip=never --incompatible_sandbox_hermetic_tmp=false --test_output=errors \
      --nocache_test_results --runs_per_test=1 --flaky_test_attempts=1 \
      --test_env=GO_TEST_WRAP_TESTV=1 "${lane_options[@]}" "${options[@]}" \
      "--build_metadata=TIMER_RLIMIT_PHASE=$phase" \
      "--build_event_json_file=$selection_dir/$phase-bep.json" \
      "$@" --target_pattern_file="$phase_targets"
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
  local initial_cgroup
  initial_cgroup=$(python3 - "$selection_dir/selection.json" "$selection_dir/targets" <<'PY'
import json
from pathlib import Path
import sys

selection = json.loads(Path(sys.argv[1]).read_text())
targets = set(Path(sys.argv[2]).read_text().splitlines())
print("true" if targets.intersection(selection.get("initial_cgroup_owners", [])) else "false")
PY
  )
  if [[ $initial_cgroup == true ]]; then
    # This route owns global cgroup settings on a disposable hosted VM. The
    # workflow runs directly on that VM, so PID 1 identifies its namespaces.
    printf 'github_actions=%s\nrunner_environment=%s\npid1_comm=%s\n' \
      "${GITHUB_ACTIONS:-}" "${RUNNER_ENVIRONMENT:-}" "$(< /proc/1/comm)" \
      | tee "$RUNNER_TEMP/qualification/initial-cgroup-namespaces.txt"
    if [[ ${GITHUB_ACTIONS:-} != true || ${RUNNER_ENVIRONMENT:-} != github-hosted || $(< /proc/1/comm) != systemd ]]; then
      printf 'Initial cgroup tests require a hosted Actions VM with systemd as PID 1.\n' >&2
      exit 1
    fi
    local coordinator_pid_ns coordinator_cgroup_ns init_pid_ns init_cgroup_ns
    coordinator_pid_ns=$(readlink -v /proc/self/ns/pid)
    coordinator_cgroup_ns=$(readlink -v /proc/self/ns/cgroup)
    # Linux gates another user's namespace links with a ptrace access check.
    init_pid_ns=$(sudo -n readlink -v /proc/1/ns/pid)
    init_cgroup_ns=$(sudo -n readlink -v /proc/1/ns/cgroup)
    printf 'coordinator_pid=%s\ninit_pid=%s\ncoordinator_cgroup=%s\ninit_cgroup=%s\n' \
      "$coordinator_pid_ns" "$init_pid_ns" "$coordinator_cgroup_ns" "$init_cgroup_ns" \
      | tee -a "$RUNNER_TEMP/qualification/initial-cgroup-namespaces.txt"
    if [[ $coordinator_pid_ns != "$init_pid_ns" || $coordinator_cgroup_ns != "$init_cgroup_ns" || -e /sys/fs/cgroup/cgroup.type ]]; then
      printf 'Initial cgroup tests require the VM PID/cgroup namespaces and hierarchy root.\n' >&2
      exit 1
    fi
    # Other local tests must not overlap changes to the root controllers or
    # mount flags. Each original shard restores its snapshot before returning.
    options+=(--local_test_jobs=1
      "--test_env=GVISOR_HOST_PID_NS=$coordinator_pid_ns"
      "--test_env=GVISOR_HOST_CGROUP_NS=$coordinator_cgroup_ns"
      "--test_env=GVISOR_HOST_MOUNT_NS=$(readlink /proc/self/ns/mnt)")
  fi
  python3 - <<'PYTIMER_BEFORE'
import base64
import hashlib
from pathlib import Path
path = Path("test/syscalls/linux/timers.cc")
assert hashlib.sha256(path.read_bytes()).hexdigest() == '0c37a60fccf01a5c5df336568ca4b3b843587352bc3d3ff1c0ef64efccec52c8'
value = base64.b64decode('Ly8gQ29weXJpZ2h0IDIwMTggVGhlIGdWaXNvciBBdXRob3JzLgovLwovLyBMaWNlbnNlZCB1bmRlciB0aGUgQXBhY2hlIExpY2Vuc2UsIFZlcnNpb24gMi4wICh0aGUgIkxpY2Vuc2UiKTsKLy8geW91IG1heSBub3QgdXNlIHRoaXMgZmlsZSBleGNlcHQgaW4gY29tcGxpYW5jZSB3aXRoIHRoZSBMaWNlbnNlLgovLyBZb3UgbWF5IG9idGFpbiBhIGNvcHkgb2YgdGhlIExpY2Vuc2UgYXQKLy8KLy8gICAgIGh0dHA6Ly93d3cuYXBhY2hlLm9yZy9saWNlbnNlcy9MSUNFTlNFLTIuMAovLwovLyBVbmxlc3MgcmVxdWlyZWQgYnkgYXBwbGljYWJsZSBsYXcgb3IgYWdyZWVkIHRvIGluIHdyaXRpbmcsIHNvZnR3YXJlCi8vIGRpc3RyaWJ1dGVkIHVuZGVyIHRoZSBMaWNlbnNlIGlzIGRpc3RyaWJ1dGVkIG9uIGFuICJBUyBJUyIgQkFTSVMsCi8vIFdJVEhPVVQgV0FSUkFOVElFUyBPUiBDT05ESVRJT05TIE9GIEFOWSBLSU5ELCBlaXRoZXIgZXhwcmVzcyBvciBpbXBsaWVkLgovLyBTZWUgdGhlIExpY2Vuc2UgZm9yIHRoZSBzcGVjaWZpYyBsYW5ndWFnZSBnb3Zlcm5pbmcgcGVybWlzc2lvbnMgYW5kCi8vIGxpbWl0YXRpb25zIHVuZGVyIHRoZSBMaWNlbnNlLgoKI2luY2x1ZGUgPGVycm5vLmg+CiNpbmNsdWRlIDxmY250bC5oPgojaW5jbHVkZSA8cG9sbC5oPgojaW5jbHVkZSA8c2lnbmFsLmg+CiNpbmNsdWRlIDxzdGRpbnQuaD4KI2luY2x1ZGUgPHN0ZGxpYi5oPgojaW5jbHVkZSA8c3lzL3ByY3RsLmg+CiNpbmNsdWRlIDxzeXMvcmVzb3VyY2UuaD4KI2luY2x1ZGUgPHN5cy90aW1lLmg+CiNpbmNsdWRlIDxzeXMvd2FpdC5oPgojaW5jbHVkZSA8c3lzY2FsbC5oPgojaW5jbHVkZSA8dGltZS5oPgojaW5jbHVkZSA8dW5pc3RkLmg+CgojaW5jbHVkZSA8YXRvbWljPgoKI2luY2x1ZGUgImdtb2NrL2dtb2NrLmgiCiNpbmNsdWRlICJndGVzdC9ndGVzdC5oIgojaW5jbHVkZSAiYWJzbC9mbGFncy9mbGFnLmgiCiNpbmNsdWRlICJhYnNsL3RpbWUvY2xvY2suaCIKI2luY2x1ZGUgImFic2wvdGltZS90aW1lLmgiCiNpbmNsdWRlICJiZW5jaG1hcmsvYmVuY2htYXJrLmgiCiNpbmNsdWRlICJ0ZXN0L3V0aWwvY2xlYW51cC5oIgojaW5jbHVkZSAidGVzdC91dGlsL2xvZ2dpbmcuaCIKI2luY2x1ZGUgInRlc3QvdXRpbC9tdWx0aXByb2Nlc3NfdXRpbC5oIgojaW5jbHVkZSAidGVzdC91dGlsL3Bvc2l4X2Vycm9yLmgiCiNpbmNsdWRlICJ0ZXN0L3V0aWwvc2F2ZV91dGlsLmgiCiNpbmNsdWRlICJ0ZXN0L3V0aWwvc2lnbmFsX3V0aWwuaCIKI2luY2x1ZGUgInRlc3QvdXRpbC90ZXN0X3V0aWwuaCIKI2luY2x1ZGUgInRlc3QvdXRpbC90aHJlYWRfdXRpbC5oIgojaW5jbHVkZSAidGVzdC91dGlsL3RpbWVyX3V0aWwuaCIKCkFCU0xfRkxBRyhib29sLCB0aW1lcnNfdGVzdF9zbGVlcCwgZmFsc2UsCiAgICAgICAgICAiSWYgdHJ1ZSwgc2xlZXAgZm9yZXZlciBpbnN0ZWFkIG9mIHJ1bm5pbmcgdGVzdHMuIik7Cgp1c2luZyA6OnRlc3Rpbmc6Ol87CnVzaW5nIDo6dGVzdGluZzo6QW55T2Y7CgpuYW1lc3BhY2UgZ3Zpc29yIHsKbmFtZXNwYWNlIHRlc3RpbmcgewpuYW1lc3BhY2UgewoKI2lmbmRlZiBDUFVDTE9DS19QUk9GCiNkZWZpbmUgQ1BVQ0xPQ0tfUFJPRiAwCiNlbmRpZiAgLy8gQ1BVQ0xPQ0tfUFJPRgoKY2xvY2tpZF90IFByb2Nlc3NDUFVDbG9jayhwaWRfdCBwaWQpIHsKICAvLyBVc2UgcGlkLXNwZWNpZmljIENQVUNMT0NLX1BST0YsIHdoaWNoIGlzIHRoZSBjbG9jayB1c2VkIHRvIGVuZm9yY2UKICAvLyBSTElNSVRfQ1BVLgogIHJldHVybiAofnN0YXRpY19jYXN0PGNsb2NraWRfdD4ocGlkKSA8PCAzKSB8IENQVUNMT0NLX1BST0Y7Cn0KClBvc2l4RXJyb3JPcjxhYnNsOjpEdXJhdGlvbj4gUHJvY2Vzc0NQVVRpbWUocGlkX3QgcGlkKSB7CiAgc3RydWN0IHRpbWVzcGVjIHRzOwogIGludCByZXQgPSBjbG9ja19nZXR0aW1lKFByb2Nlc3NDUFVDbG9jayhwaWQpLCAmdHMpOwogIGlmIChyZXQgPCAwKSB7CiAgICByZXR1cm4gUG9zaXhFcnJvcihlcnJubywgImNsb2NrX2dldHRpbWUgZmFpbGVkIik7CiAgfQoKICByZXR1cm4gYWJzbDo6RHVyYXRpb25Gcm9tVGltZXNwZWModHMpOwp9Cgp2b2lkIE5vb3BTaWduYWxIYW5kbGVyKGludCBzaWdubykgewogIFRFU1RfQ0hFQ0tfTVNHKFNJR1hDUFUgPT0gc2lnbm8sCiAgICAgICAgICAgICAgICAgIk5vb3BTaWdIYW5kbGVyIGRpZCBub3QgcmVjZWl2ZSBleHBlY3RlZCBzaWduYWwiKTsKfQoKdm9pZCBVbmluc3RhbGxpbmdTaWduYWxIYW5kbGVyKGludCBzaWdubykgewogIFRFU1RfQ0hFQ0tfTVNHKFNJR1hDUFUgPT0gc2lnbm8sCiAgICAgICAgICAgICAgICAgIlVuaW5zdGFsbGluZ1NpZ25hbEhhbmRsZXIgZGlkIG5vdCByZWNlaXZlIGV4cGVjdGVkIHNpZ25hbCIpOwogIHN0cnVjdCBzaWdhY3Rpb24gcmV2X2FjdGlvbjsKICByZXZfYWN0aW9uLnNhX2hhbmRsZXIgPSBTSUdfREZMOwogIHJldl9hY3Rpb24uc2FfZmxhZ3MgPSAwOwogIHNpZ2VtcHR5c2V0KCZyZXZfYWN0aW9uLnNhX21hc2spOwogIHNpZ2FjdGlvbihTSUdYQ1BVLCAmcmV2X2FjdGlvbiwgbnVsbHB0cik7Cn0KClRFU1QoVGltZXJUZXN0LCBQcm9jZXNzS2lsbGVkT25DUFVTb2Z0TGltaXQpIHsKICBjb25zdGV4cHIgYWJzbDo6RHVyYXRpb24ga1NvZnRMaW1pdCA9IGFic2w6OlNlY29uZHMoMSk7CiAgY29uc3RleHByIGFic2w6OkR1cmF0aW9uIGtIYXJkTGltaXQgPSBhYnNsOjpTZWNvbmRzKDMpOwoKICBzdHJ1Y3QgcmxpbWl0IGNwdV9saW1pdHM7CiAgY3B1X2xpbWl0cy5ybGltX2N1ciA9IGFic2w6OlRvSW50NjRTZWNvbmRzKGtTb2Z0TGltaXQpOwogIGNwdV9saW1pdHMucmxpbV9tYXggPSBhYnNsOjpUb0ludDY0U2Vjb25kcyhrSGFyZExpbWl0KTsKCiAgaW50IHBpZCA9IGZvcmsoKTsKICBNYXliZVNhdmUoKTsKICBpZiAocGlkID09IDApIHsKICAgIFRFU1RfUENIRUNLKHNldHJsaW1pdChSTElNSVRfQ1BVLCAmY3B1X2xpbWl0cykgPT0gMCk7CiAgICBNYXliZVNhdmUoKTsKICAgIGZvciAoOzspIHsKICAgICAgaW50IHggPSAwOwogICAgICBiZW5jaG1hcms6OkRvTm90T3B0aW1pemUoeCk7ICAvLyBEb24ndCBvcHRpbWl6ZSB0aGlzIGxvb3AgYXdheS4KICAgIH0KICB9CiAgQVNTRVJUX1RIQVQocGlkLCBTeXNjYWxsU3VjY2VlZHMoKSk7CiAgYXV0byBjID0gQ2xlYW51cChbcGlkXSB7CiAgICBpbnQgc3RhdHVzOwogICAgRVhQRUNUX1RIQVQod2FpdHBpZChwaWQsICZzdGF0dXMsIDApLCBTeXNjYWxsU3VjY2VlZHNXaXRoVmFsdWUocGlkKSk7CiAgICBFWFBFQ1RfVFJVRShXSUZTSUdOQUxFRChzdGF0dXMpKTsKICAgIEVYUEVDVF9FUShXVEVSTVNJRyhzdGF0dXMpLCBTSUdYQ1BVKTsKICB9KTsKCiAgLy8gV2FpdCBmb3IgdGhlIGNoaWxkIHRvIGV4aXQsIGJ1dCBkbyBub3QgcmVhcCBpdC4gVGhpcyB3aWxsIGFsbG93IHVzIHRvIGNoZWNrCiAgLy8gaXRzIENQVSB1c2FnZSB3aGlsZSBpdCBpcyB6b21iaWVkLgogIEVYUEVDVF9USEFUKHdhaXRpZChQX1BJRCwgcGlkLCBudWxscHRyLCBXRVhJVEVEIHwgV05PV0FJVCksCiAgICAgICAgICAgICAgU3lzY2FsbFN1Y2NlZWRzKCkpOwoKICAvLyBBc3NlcnQgdGhhdCB0aGUgY2hpbGQgc3BlbnQgMXMgb2YgQ1BVIGJlZm9yZSBnZXR0aW5nIGtpbGxlZC4KICAvLwogIC8vIFdlIG11c3QgYmUgY2FyZWZ1bCB0byB1c2UgQ1BVQ0xPQ0tfUFJPRiwgdGhlIHNhbWUgY2xvY2sgdXNlZCBmb3IgUkxJTUlUX0NQVQogIC8vIGVuZm9yY2VtZW50LCB0byBnZXQgY29ycmVjdCByZXN1bHRzLiBOb3RlIHRoYXQgdGhpcyBpcyBzbGlnaHRseSBkaWZmZXJlbnQKICAvLyBmcm9tIHJ1c2FnZS1yZXBvcnRlZCBDUFUgdXNhZ2U6CiAgLy8KICAvLyBSTElNSVRfQ1BVLCBDUFVDTE9DS19QUk9GIHVzZSBrZXJuZWwvc2NoZWQvY3B1dGltZS5jOnRocmVhZF9ncm91cF9jcHV0aW1lLgogIC8vIHJ1c2FnZSB1c2VzIGtlcm5lbC9zY2hlZC9jcHV0aW1lLmM6dGhyZWFkX2dyb3VwX2NwdXRpbWVfYWRqdXN0ZWQuCiAgYWJzbDo6RHVyYXRpb24gY3B1ID0gQVNTRVJUX05PX0VSUk5PX0FORF9WQUxVRShQcm9jZXNzQ1BVVGltZShwaWQpKTsKICBFWFBFQ1RfR0UoY3B1LCBrU29mdExpbWl0KTsKCiAgLy8gQ2hpbGQgZGlkIG5vdCBtYWtlIGl0IHRvIHRoZSBoYXJkIGxpbWl0LgogIC8vCiAgLy8gTGludXggc2VuZHMgU0lHWENQVSBzeW5jaHJvbm91c2x5IHdpdGggQ1BVIHRpY2sgdXBkYXRlcy4gU2VlCiAgLy8ga2VybmVsL3RpbWUvdGltZXIuYzp1cGRhdGVfcHJvY2Vzc190aW1lczoKICAvLyAgID0+IGFjY291bnRfcHJvY2Vzc190aWNrICAvLyB1cGRhdGUgdGFzayBDUFUgdXNhZ2UuCiAgLy8gICA9PiBydW5fcG9zaXhfY3B1X3RpbWVycyAgLy8gZW5mb3JjZSBSTElNSVRfQ1BVLCBzZW5kaW5nIHNpZ25hbC4KICAvLwogIC8vIFRodXMsIG9ubHkgY2hhbmNlIGZvciB0aGlzIHRvIGZsYWtlIGlzIGlmIHRoZSBzeXN0ZW0gdGltZSByZXF1aXJlZCB0bwogIC8vIGRlbGl2ZXIgdGhlIHNpZ25hbCBleGNlZWRzIDJzLgogIEVYUEVDVF9MVChjcHUsIGtIYXJkTGltaXQpOwp9CgpURVNUKFRpbWVyVGVzdCwgUHJvY2Vzc1BpbmdlZFJlcGVhdGVkbHlBZnRlckNQVVNvZnRMaW1pdCkgewogIHN0cnVjdCBzaWdhY3Rpb24gbmV3X2FjdGlvbjsKICBuZXdfYWN0aW9uLnNhX2hhbmRsZXIgPSBVbmluc3RhbGxpbmdTaWduYWxIYW5kbGVyOwogIG5ld19hY3Rpb24uc2FfZmxhZ3MgPSAwOwogIHNpZ2VtcHR5c2V0KCZuZXdfYWN0aW9uLnNhX21hc2spOwoKICBjb25zdGV4cHIgYWJzbDo6RHVyYXRpb24ga1NvZnRMaW1pdCA9IGFic2w6OlNlY29uZHMoMSk7CiAgY29uc3RleHByIGFic2w6OkR1cmF0aW9uIGtIYXJkTGltaXQgPSBhYnNsOjpTZWNvbmRzKDEwKTsKCiAgc3RydWN0IHJsaW1pdCBjcHVfbGltaXRzOwogIGNwdV9saW1pdHMucmxpbV9jdXIgPSBhYnNsOjpUb0ludDY0U2Vjb25kcyhrU29mdExpbWl0KTsKICBjcHVfbGltaXRzLnJsaW1fbWF4ID0gYWJzbDo6VG9JbnQ2NFNlY29uZHMoa0hhcmRMaW1pdCk7CgogIGludCBwaWQgPSBmb3JrKCk7CiAgTWF5YmVTYXZlKCk7CiAgaWYgKHBpZCA9PSAwKSB7CiAgICBURVNUX1BDSEVDSyhzaWdhY3Rpb24oU0lHWENQVSwgJm5ld19hY3Rpb24sIG51bGxwdHIpID09IDApOwogICAgTWF5YmVTYXZlKCk7CiAgICBURVNUX1BDSEVDSyhzZXRybGltaXQoUkxJTUlUX0NQVSwgJmNwdV9saW1pdHMpID09IDApOwogICAgTWF5YmVTYXZlKCk7CiAgICBmb3IgKDs7KSB7CiAgICAgIGludCB4ID0gMDsKICAgICAgYmVuY2htYXJrOjpEb05vdE9wdGltaXplKHgpOyAgLy8gRG9uJ3Qgb3B0aW1pemUgdGhpcyBsb29wIGF3YXkuCiAgICB9CiAgfQogIEFTU0VSVF9USEFUKHBpZCwgU3lzY2FsbFN1Y2NlZWRzKCkpOwogIGF1dG8gYyA9IENsZWFudXAoW3BpZF0gewogICAgaW50IHN0YXR1czsKICAgIEVYUEVDVF9USEFUKHdhaXRwaWQocGlkLCAmc3RhdHVzLCAwKSwgU3lzY2FsbFN1Y2NlZWRzV2l0aFZhbHVlKHBpZCkpOwogICAgRVhQRUNUX1RSVUUoV0lGU0lHTkFMRUQoc3RhdHVzKSk7CiAgICBFWFBFQ1RfRVEoV1RFUk1TSUcoc3RhdHVzKSwgU0lHWENQVSk7CiAgfSk7CgogIC8vIFdhaXQgZm9yIHRoZSBjaGlsZCB0byBleGl0LCBidXQgZG8gbm90IHJlYXAgaXQuIFRoaXMgd2lsbCBhbGxvdyB1cyB0byBjaGVjawogIC8vIGl0cyBDUFUgdXNhZ2Ugd2hpbGUgaXQgaXMgem9tYmllZC4KICBFWFBFQ1RfVEhBVCh3YWl0aWQoUF9QSUQsIHBpZCwgbnVsbHB0ciwgV0VYSVRFRCB8IFdOT1dBSVQpLAogICAgICAgICAgICAgIFN5c2NhbGxTdWNjZWVkcygpKTsKCiAgYWJzbDo6RHVyYXRpb24gY3B1ID0gQVNTRVJUX05PX0VSUk5PX0FORF9WQUxVRShQcm9jZXNzQ1BVVGltZShwaWQpKTsKICAvLyBGb2xsb3dpbmcgc2lnbmFscyBjb21lIGV2ZXJ5IENQVSBzZWNvbmQuCiAgRVhQRUNUX0dFKGNwdSwga1NvZnRMaW1pdCArIGFic2w6OlNlY29uZHMoMSkpOwoKICAvLyBDaGlsZCBkaWQgbm90IG1ha2UgaXQgdG8gdGhlIGhhcmQgbGltaXQuCiAgLy8KICAvLyBBcyBhYm92ZSwgc2hvdWxkIG5vdCBmbGFrZS4KICBFWFBFQ1RfTFQoY3B1LCBrSGFyZExpbWl0KTsKfQoKVEVTVChUaW1lclRlc3QsIFByb2Nlc3NLaWxsZWRPbkNQVUhhcmRMaW1pdCkgewogIHN0cnVjdCBzaWdhY3Rpb24gbmV3X2FjdGlvbjsKICBuZXdfYWN0aW9uLnNhX2hhbmRsZXIgPSBOb29wU2lnbmFsSGFuZGxlcjsKICBuZXdfYWN0aW9uLnNhX2ZsYWdzID0gMDsKICBzaWdlbXB0eXNldCgmbmV3X2FjdGlvbi5zYV9tYXNrKTsKCiAgY29uc3RleHByIGFic2w6OkR1cmF0aW9uIGtTb2Z0TGltaXQgPSBhYnNsOjpTZWNvbmRzKDEpOwogIGNvbnN0ZXhwciBhYnNsOjpEdXJhdGlvbiBrSGFyZExpbWl0ID0gYWJzbDo6U2Vjb25kcygzKTsKCiAgc3RydWN0IHJsaW1pdCBjcHVfbGltaXRzOwogIGNwdV9saW1pdHMucmxpbV9jdXIgPSBhYnNsOjpUb0ludDY0U2Vjb25kcyhrU29mdExpbWl0KTsKICBjcHVfbGltaXRzLnJsaW1fbWF4ID0gYWJzbDo6VG9JbnQ2NFNlY29uZHMoa0hhcmRMaW1pdCk7CgogIGludCBwaWQgPSBmb3JrKCk7CiAgTWF5YmVTYXZlKCk7CiAgaWYgKHBpZCA9PSAwKSB7CiAgICBURVNUX1BDSEVDSyhzaWdhY3Rpb24oU0lHWENQVSwgJm5ld19hY3Rpb24sIG51bGxwdHIpID09IDApOwogICAgTWF5YmVTYXZlKCk7CiAgICBURVNUX1BDSEVDSyhzZXRybGltaXQoUkxJTUlUX0NQVSwgJmNwdV9saW1pdHMpID09IDApOwogICAgTWF5YmVTYXZlKCk7CiAgICBmb3IgKDs7KSB7CiAgICAgIGludCB4ID0gMDsKICAgICAgYmVuY2htYXJrOjpEb05vdE9wdGltaXplKHgpOyAgLy8gRG9uJ3Qgb3B0aW1pemUgdGhpcyBsb29wIGF3YXkuCiAgICB9CiAgfQogIEFTU0VSVF9USEFUKHBpZCwgU3lzY2FsbFN1Y2NlZWRzKCkpOwogIGF1dG8gYyA9IENsZWFudXAoW3BpZF0gewogICAgaW50IHN0YXR1czsKICAgIEVYUEVDVF9USEFUKHdhaXRwaWQocGlkLCAmc3RhdHVzLCAwKSwgU3lzY2FsbFN1Y2NlZWRzV2l0aFZhbHVlKHBpZCkpOwogICAgRVhQRUNUX1RSVUUoV0lGU0lHTkFMRUQoc3RhdHVzKSk7CiAgICBFWFBFQ1RfRVEoV1RFUk1TSUcoc3RhdHVzKSwgU0lHS0lMTCk7CiAgfSk7CgogIC8vIFdhaXQgZm9yIHRoZSBjaGlsZCB0byBleGl0LCBidXQgZG8gbm90IHJlYXAgaXQuIFRoaXMgd2lsbCBhbGxvdyB1cyB0byBjaGVjawogIC8vIGl0cyBDUFUgdXNhZ2Ugd2hpbGUgaXQgaXMgem9tYmllZC4KICBFWFBFQ1RfVEhBVCh3YWl0aWQoUF9QSUQsIHBpZCwgbnVsbHB0ciwgV0VYSVRFRCB8IFdOT1dBSVQpLAogICAgICAgICAgICAgIFN5c2NhbGxTdWNjZWVkcygpKTsKCiAgYWJzbDo6RHVyYXRpb24gY3B1ID0gQVNTRVJUX05PX0VSUk5PX0FORF9WQUxVRShQcm9jZXNzQ1BVVGltZShwaWQpKTsKICBFWFBFQ1RfR0UoY3B1LCBrSGFyZExpbWl0KTsKfQoKVEVTVChUaW1lclRlc3QsIFJsaW1pdENwdUluaGVyaXRlZEFjcm9zc0ZvcmspIHsKICBjb25zdCBjaGFyKiBvdXRwdXRfZGlyID0gZ2V0ZW52KCJURVNUX1VOREVDTEFSRURfT1VUUFVUU19ESVIiKTsKICBBU1NFUlRfTkUob3V0cHV0X2RpciwgbnVsbHB0cik7CiAgaW50IGRpcl9mZCA9IG9wZW4ob3V0cHV0X2RpciwgT19ESVJFQ1RPUlkgfCBPX1JET05MWSB8IE9fQ0xPRVhFQyk7CiAgQVNTRVJUX1RIQVQoZGlyX2ZkLCBTeXNjYWxsU3VjY2VlZHMoKSk7CiAgYXV0byBjbG9zZV9kaXIgPSBDbGVhbnVwKFtkaXJfZmRdIHsgY2xvc2UoZGlyX2ZkKTsgfSk7CiAgaW50IHNhbXBsZV9mZCA9IG9wZW5hdChkaXJfZmQsICJybGltaXQtY2xvY2suYmluIiwKICAgICAgICAgICAgICAgICAgICAgICAgIE9fV1JPTkxZIHwgT19DUkVBVCB8IE9fRVhDTCB8IE9fQ0xPRVhFQywgMDYwMCk7CiAgQVNTRVJUX1RIQVQoc2FtcGxlX2ZkLCBTeXNjYWxsU3VjY2VlZHMoKSk7CiAgYXV0byBjbG9zZV9zYW1wbGVzID0gQ2xlYW51cChbc2FtcGxlX2ZkXSB7IGNsb3NlKHNhbXBsZV9mZCk7IH0pOwogIC8vIEZvcmstb25seSBkaWFnbm9zdGljOiBmaXhlZCBsaXR0bGUtZW5kaWFuIHVpbnQ2NF90IHJlY29yZHMgb24gQU1ENjQuCiAgLy8gdmVyc2lvbiwga2luZCwgcGlkLCBsb29wcywgY29tcGxldGVkIHBvbGxzLCB3YWxsIG5zLCBQUk9GIG5zLCBTQ0hFRCBucywKICAvLyBjdXJyZW50IHNvZnQvaGFyZCBDUFUgbGltaXRzLCBjdXJyZW50IHRpbWVyIHNsYWNrIG5zLgogIGF1dG8gc2FtcGxlID0gW3NhbXBsZV9mZF0ocGlkX3QgcGlkLCB1aW50NjRfdCBraW5kLCB1aW50NjRfdCBsb29wcywKICAgICAgICAgICAgICAgICAgICAgICAgICAgIHVpbnQ2NF90IHBvbGxzKSB7CiAgICBzdHJ1Y3QgdGltZXNwZWMgd2FsbCwgcHJvZiwgc2NoZWQ7CiAgICBURVNUX1BDSEVDSyhjbG9ja19nZXR0aW1lKENMT0NLX01PTk9UT05JQywgJndhbGwpID09IDApOwogICAgVEVTVF9QQ0hFQ0soY2xvY2tfZ2V0dGltZShQcm9jZXNzQ1BVQ2xvY2socGlkKSwgJnByb2YpID09IDApOwogICAgVEVTVF9QQ0hFQ0soY2xvY2tfZ2V0dGltZShQcm9jZXNzQ1BVQ2xvY2socGlkKSB8IDIsICZzY2hlZCkgPT0gMCk7CiAgICBzdHJ1Y3QgcmxpbWl0IGxpbWl0OwogICAgVEVTVF9QQ0hFQ0soZ2V0cmxpbWl0KFJMSU1JVF9DUFUsICZsaW1pdCkgPT0gMCk7CiAgICBsb25nIHNsYWNrID0gc3lzY2FsbChTWVNfcHJjdGwsIFBSX0dFVF9USU1FUlNMQUNLLCAwVUwsIDBVTCwgMFVMLCAwVUwpOwogICAgVEVTVF9QQ0hFQ0soc2xhY2sgPj0gMCk7CiAgICB1aW50NjRfdCB2YWx1ZXNbXSA9IHsKICAgICAgICAxLCBraW5kLCBzdGF0aWNfY2FzdDx1aW50NjRfdD4ocGlkKSwgbG9vcHMsIHBvbGxzLAogICAgICAgIHN0YXRpY19jYXN0PHVpbnQ2NF90Pih3YWxsLnR2X3NlYykgKiAxMDAwMDAwMDAwICsgd2FsbC50dl9uc2VjLAogICAgICAgIHN0YXRpY19jYXN0PHVpbnQ2NF90Pihwcm9mLnR2X3NlYykgKiAxMDAwMDAwMDAwICsgcHJvZi50dl9uc2VjLAogICAgICAgIHN0YXRpY19jYXN0PHVpbnQ2NF90PihzY2hlZC50dl9zZWMpICogMTAwMDAwMDAwMCArIHNjaGVkLnR2X25zZWMsCiAgICAgICAgbGltaXQucmxpbV9jdXIsIGxpbWl0LnJsaW1fbWF4LCBzdGF0aWNfY2FzdDx1aW50NjRfdD4oc2xhY2spfTsKICAgIFRFU1RfUENIRUNLKFJldHJ5RUlOVFIod3JpdGUpKHNhbXBsZV9mZCwgdmFsdWVzLCBzaXplb2YodmFsdWVzKSkgPT0KICAgICAgICAgICAgICAgIHN0YXRpY19jYXN0PHNzaXplX3Q+KHNpemVvZih2YWx1ZXMpKSk7CiAgfTsKICBwaWRfdCBjaGlsZF9waWQgPSBmb3JrKCk7CiAgTWF5YmVTYXZlKCk7CiAgaWYgKGNoaWxkX3BpZCA9PSAwKSB7CiAgICAvLyBJZ25vcmUgU0lHWENQVSBmcm9tIHRoZSBSTElNSVRfQ1BVIHNvZnQgbGltaXQuCiAgICBzdHJ1Y3Qgc2lnYWN0aW9uIG5ld19hY3Rpb247CiAgICBuZXdfYWN0aW9uLnNhX2hhbmRsZXIgPSBOb29wU2lnbmFsSGFuZGxlcjsKICAgIG5ld19hY3Rpb24uc2FfZmxhZ3MgPSAwOwogICAgc2lnZW1wdHlzZXQoJm5ld19hY3Rpb24uc2FfbWFzayk7CiAgICBURVNUX1BDSEVDSyhzaWdhY3Rpb24oU0lHWENQVSwgJm5ld19hY3Rpb24sIG51bGxwdHIpID09IDApOwoKICAgIGNvbnN0ZXhwciBpbnQga0RlbGF5U2Vjb25kcyA9IDI7CiAgICBzdHJ1Y3QgdGltZXNwZWMgdHM7CiAgICBURVNUX1BDSEVDSyhjbG9ja19nZXR0aW1lKENMT0NLX1BST0NFU1NfQ1BVVElNRV9JRCwgJnRzKSA9PSAwKTsKICAgIHN0cnVjdCBybGltaXQgY3B1X2xpbWl0czsKICAgIC8vIFNldCBzb2Z0IGxpbWl0IHRvIDAgdG8gZXhwaXJlIGltbWVkaWF0ZWx5LiBUaGlzIHNob3VsZCBjYXVzZQogICAgLy8gYSBTSUdYQ1BVIHRvIGJlIHNlbnQgdG8gdGhlIGdyYW5kY2hpbGQgaW1tZWRpYXRlbHkgb24gZm9yay4KICAgIGNwdV9saW1pdHMucmxpbV9jdXIgPSAwOwogICAgLy8gU2V0IGhhcmQgbGltaXQgdG8gZXhwaXJlIGEgc2hvcnQgdGltZSBmcm9tIG5vdy4gKFNpbmNlIHdlCiAgICAvLyBtYXkgbm90IGJlIGFibGUgdG8gcmFpc2UgUkxJTUlUX0NQVSBhZ2FpbiwgdGhpcyBtdXN0IGhhcHBlbiBpbiBhCiAgICAvLyBkaXNwb3NhYmxlIGNoaWxkIG9mIHRoZSB0ZXN0IHByb2Nlc3MuKQogICAgLy8gKzEgdG8gcm91bmQgdXAsIHByZXN1bWluZyB0aGF0IHRzLnR2X25zZWMgPiAwLgogICAgY3B1X2xpbWl0cy5ybGltX21heCA9IHRzLnR2X3NlYyArIGtEZWxheVNlY29uZHMgKyAxOwogICAgVEVTVF9QQ0hFQ0soc2V0cmxpbWl0KFJMSU1JVF9DUFUsICZjcHVfbGltaXRzKSA9PSAwKTsKICAgIE1heWJlU2F2ZSgpOwoKICAgIHBpZF90IGdyYW5kY2hpbGRfcGlkID0gZm9yaygpOwogICAgTWF5YmVTYXZlKCk7CiAgICBpZiAoZ3JhbmRjaGlsZF9waWQgPT0gMCkgewogICAgICBzYW1wbGUoZ2V0cGlkKCksIDAsIDAsIDApOwogICAgICBpbnQgcGlwZWZkWzJdOwogICAgICBURVNUX1BDSEVDSyhwaXBlKHBpcGVmZCkgPT0gMCk7CiAgICAgIHN0cnVjdCBwb2xsZmQgcGZkOwogICAgICBwZmQuZmQgPSBwaXBlZmRbMF07CiAgICAgIHBmZC5ldmVudHMgPSBQT0xMSU47CiAgICAgIHN0cnVjdCB0aW1lc3BlYyB0aW1lb3V0OwogICAgICB0aW1lb3V0LnR2X3NlYyA9IDA7CiAgICAgIHRpbWVvdXQudHZfbnNlYyA9IDEwMDA7CgogICAgICAvLyBCdXJuIENQVS4KICAgICAgdWludDY0X3QgeCA9IDA7CiAgICAgIGZvciAoOzspIHsKICAgICAgICB4Kys7CiAgICAgICAgYmVuY2htYXJrOjpEb05vdE9wdGltaXplKHgpOyAgLy8gRG9uJ3Qgb3B0aW1pemUgdGhpcyBsb29wIGF3YXkuCiAgICAgICAgLy8gUGVyaW9kaWNhbGx5IGJsb2NrIHRvIGVuc3VyZSB0aGF0IGNoaWxkX3BpZCBnZXRzIGEgY2hhbmNlIHRvIHJ1biBhbmQKICAgICAgICAvLyBibG9jayBpbiB3YWl0aWQoKS4KICAgICAgICAvLyBUT0RPOiBiLzMxNTM4ODkyOSAtIHJlbW92ZSB0aGlzCiAgICAgICAgaWYgKHggJSAxNjM4NCA9PSAwKSB7CiAgICAgICAgICBURVNUX1BDSEVDSyhSZXRyeUVJTlRSKHBwb2xsKSgmcGZkLCAxLCAmdGltZW91dCwgbnVsbHB0cikgPT0gMCk7CiAgICAgICAgICAvLyBUaHJlZSBjbG9jayByZWFkcyBwZXIgMTYzODQgY29tcGxldGVkIHBvbGxzLCBub3QgcGVyIGJ1c3kgbG9vcC4KICAgICAgICAgIGlmICh4ICUgKHVpbnQ2NF90ezE2Mzg0fSAqIDE2Mzg0KSA9PSAwKSB7CiAgICAgICAgICAgIHNhbXBsZShnZXRwaWQoKSwgMSwgeCwgeCAvIDE2Mzg0KTsKICAgICAgICAgIH0KICAgICAgICB9CiAgICAgIH0KICAgIH0KICAgIFRFU1RfUENIRUNLKGdyYW5kY2hpbGRfcGlkID4gMCk7CgogICAgLy8gV2FpdCBmb3IgdGhlIGdyYW5kY2hpbGQgdG8gZXhpdCwgYnV0IGRvIG5vdCByZWFwIGl0LiBUaGlzIHdpbGwgYWxsb3cgdXMKICAgIC8vIHRvIGNoZWNrIGl0cyBDUFUgdXNhZ2Ugd2hpbGUgaXQgaXMgem9tYmllZC4KICAgIFRFU1RfUENIRUNLKHdhaXRpZChQX1BJRCwgZ3JhbmRjaGlsZF9waWQsIG51bGxwdHIsIFdFWElURUQgfCBXTk9XQUlUKSA9PSAwKTsKICAgIC8vIFRoZSBmaW5hbCByZWNvcmQgcmVhZHMgdGhlIHpvbWJpZSdzIGNsb2Nrcy4gTGltaXRzL3NsYWNrIGluIHRoaXMgcm93CiAgICAvLyBiZWxvbmcgdG8gaXRzIHBhcmVudDsgb25seSBraW5kIDAvMSByb3dzIGRlc2NyaWJlIHRoZSBncmFuZGNoaWxkJ3MgcG9saWN5LgogICAgc2FtcGxlKGdyYW5kY2hpbGRfcGlkLCAyLCAwLCAwKTsKICAgIE1heWJlU2F2ZSgpOwogICAgVEVTVF9QQ0hFQ0soY2xvY2tfZ2V0dGltZShQcm9jZXNzQ1BVQ2xvY2soZ3JhbmRjaGlsZF9waWQpLCAmdHMpID09IDApOwogICAgVEVTVF9DSEVDSyh0cy50dl9zZWMgPj0gc3RhdGljX2Nhc3Q8bG9uZz4oY3B1X2xpbWl0cy5ybGltX21heCkpOwogICAgLy8gUmVhcCB0aGUgZ3JhbmRjaGlsZCBhbmQgY2hlY2sgdGhhdCBpdCB3YXMgU0lHS0lMTGVkIGJ5IHRoZSBSTElNSVRfQ1BVCiAgICAvLyBoYXJkIGxpbWl0LgogICAgaW50IHN0YXR1czsKICAgIFRFU1RfUENIRUNLKHdhaXRwaWQoZ3JhbmRjaGlsZF9waWQsICZzdGF0dXMsIDApID09IGdyYW5kY2hpbGRfcGlkKTsKICAgIFRFU1RfQ0hFQ0soV0lGU0lHTkFMRUQoc3RhdHVzKSAmJiAoV1RFUk1TSUcoc3RhdHVzKSA9PSBTSUdLSUxMKSk7CiAgICBfZXhpdCgwKTsKICB9CgogIGludCBzdGF0dXM7CiAgQVNTRVJUX1RIQVQod2FpdHBpZChjaGlsZF9waWQsICZzdGF0dXMsIDApLAogICAgICAgICAgICAgIFN5c2NhbGxTdWNjZWVkc1dpdGhWYWx1ZShjaGlsZF9waWQpKTsKICBFWFBFQ1RfVFJVRShXSUZFWElURUQoc3RhdHVzKSAmJiAoV0VYSVRTVEFUVVMoc3RhdHVzKSA9PSAwKSkKICAgICAgPDwgInN0YXR1cyA9ICIgPDwgc3RhdHVzOwp9CgovLyBTZWUgdGltZXJmZC5jYzpUaW1lclNsYWNrKCkgZm9yIHJhdGlvbmFsZS4KY29uc3RleHByIGFic2w6OkR1cmF0aW9uIGtUaW1lclNsYWNrID0gYWJzbDo6TWlsbGlzZWNvbmRzKDUwMCk7CgpURVNUKEludGVydmFsVGltZXJUZXN0LCBJc0luaXRpYWxseVN0b3BwZWQpIHsKICBzdHJ1Y3Qgc2lnZXZlbnQgc2V2ID0ge307CiAgc2V2LnNpZ2V2X25vdGlmeSA9IFNJR0VWX05PTkU7CiAgY29uc3QgYXV0byB0aW1lciA9CiAgICAgIEFTU0VSVF9OT19FUlJOT19BTkRfVkFMVUUoVGltZXJDcmVhdGUoQ0xPQ0tfTU9OT1RPTklDLCBzZXYpKTsKICBjb25zdCBzdHJ1Y3QgaXRpbWVyc3BlYyBpdHMgPSBBU1NFUlRfTk9fRVJSTk9fQU5EX1ZBTFVFKHRpbWVyLkdldCgpKTsKICBFWFBFQ1RfRVEoMCwgaXRzLml0X3ZhbHVlLnR2X3NlYyk7CiAgRVhQRUNUX0VRKDAsIGl0cy5pdF92YWx1ZS50dl9uc2VjKTsKfQoKLy8gS2VybmVsIGNhbiBjcmVhdGUgbXVsdGlwbGUgdGltZXJzIHdpdGhvdXQgaXNzdWUuCi8vCi8vIFJlZ3Jlc3Npb24gdGVzdCBmb3IgZ3Zpc29yLmRldi9pc3N1ZS8xNzM4LgpURVNUKEludGVydmFsVGltZXJUZXN0LCBNdWx0aXBsZVRpbWVycykgewogIHN0cnVjdCBzaWdldmVudCBzZXYgPSB7fTsKICBzZXYuc2lnZXZfbm90aWZ5ID0gU0lHRVZfTk9ORTsKICBjb25zdCBhdXRvIHRpbWVyMSA9CiAgICAgIEFTU0VSVF9OT19FUlJOT19BTkRfVkFMVUUoVGltZXJDcmVhdGUoQ0xPQ0tfTU9OT1RPTklDLCBzZXYpKTsKICBjb25zdCBhdXRvIHRpbWVyMiA9CiAgICAgIEFTU0VSVF9OT19FUlJOT19BTkRfVkFMVUUoVGltZXJDcmVhdGUoQ0xPQ0tfTU9OT1RPTklDLCBzZXYpKTsKfQoKVEVTVChJbnRlcnZhbFRpbWVyVGVzdCwgU2luZ2xlU2hvdFNpbGVudCkgewogIHN0cnVjdCBzaWdldmVudCBzZXYgPSB7fTsKICBzZXYuc2lnZXZfbm90aWZ5ID0gU0lHRVZfTk9ORTsKICBjb25zdCBhdXRvIHRpbWVyID0KICAgICAgQVNTRVJUX05PX0VSUk5PX0FORF9WQUxVRShUaW1lckNyZWF0ZShDTE9DS19NT05PVE9OSUMsIHNldikpOwoKICBjb25zdGV4cHIgYWJzbDo6RHVyYXRpb24ga0RlbGF5ID0gYWJzbDo6U2Vjb25kcygxKTsKICBzdHJ1Y3QgaXRpbWVyc3BlYyBpdHMgPSB7fTsKICBpdHMuaXRfdmFsdWUgPSBhYnNsOjpUb1RpbWVzcGVjKGtEZWxheSk7CiAgQVNTRVJUX05PX0VSUk5PKHRpbWVyLlNldCgwLCBpdHMpKTsKCiAgLy8gVGhlIHRpbWVyIHNob3VsZCBjb3VudCBkb3duIHRvIDAgYW5kIHN0b3Agc2luY2UgdGhlIGludGVydmFsIGlzIHplcm8uIE5vCiAgLy8gb3ZlcnJ1bnMgc2hvdWxkIGJlIGNvdW50ZWQuCiAgYWJzbDo6U2xlZXBGb3Ioa0RlbGF5ICsga1RpbWVyU2xhY2spOwogIGl0cyA9IEFTU0VSVF9OT19FUlJOT19BTkRfVkFMVUUodGltZXIuR2V0KCkpOwogIEVYUEVDVF9FUSgwLCBpdHMuaXRfdmFsdWUudHZfc2VjKTsKICBFWFBFQ1RfRVEoMCwgaXRzLml0X3ZhbHVlLnR2X25zZWMpOwogIEVYUEVDVF9USEFUKHRpbWVyLk92ZXJydW5zKCksIElzUG9zaXhFcnJvck9rQW5kSG9sZHMoMCkpOwp9CgpURVNUKEludGVydmFsVGltZXJUZXN0LCBQZXJpb2RpY1NpbGVudCkgewogIHN0cnVjdCBzaWdldmVudCBzZXYgPSB7fTsKICBzZXYuc2lnZXZfbm90aWZ5ID0gU0lHRVZfTk9ORTsKICBjb25zdCBhdXRvIHRpbWVyID0KICAgICAgQVNTRVJUX05PX0VSUk5PX0FORF9WQUxVRShUaW1lckNyZWF0ZShDTE9DS19NT05PVE9OSUMsIHNldikpOwoKICBjb25zdGV4cHIgYWJzbDo6RHVyYXRpb24ga1BlcmlvZCA9IGFic2w6OlNlY29uZHMoMSk7CiAgc3RydWN0IGl0aW1lcnNwZWMgaXRzID0ge307CiAgaXRzLml0X3ZhbHVlID0gaXRzLml0X2ludGVydmFsID0gYWJzbDo6VG9UaW1lc3BlYyhrUGVyaW9kKTsKICBBU1NFUlRfTk9fRVJSTk8odGltZXIuU2V0KDAsIGl0cykpOwoKICBhYnNsOjpTbGVlcEZvcihrUGVyaW9kICogMyArIGtUaW1lclNsYWNrKTsKCiAgLy8gVGhlIHRpbWVyIHNob3VsZCBzdGlsbCBiZSBydW5uaW5nLgogIGl0cyA9IEFTU0VSVF9OT19FUlJOT19BTkRfVkFMVUUodGltZXIuR2V0KCkpOwogIEVYUEVDVF9UUlVFKGl0cy5pdF92YWx1ZS50dl9uc2VjICE9IDAgfHwgaXRzLml0X3ZhbHVlLnR2X3NlYyAhPSAwKTsKCiAgLy8gVGltZXIgZXhwaXJhdGlvbnMgYXJlIG5vdCBjb3VudGVkIGFzIG92ZXJydW5zIHVuZGVyIFNJR0VWX05PTkUuCiAgRVhQRUNUX1RIQVQodGltZXIuT3ZlcnJ1bnMoKSwgSXNQb3NpeEVycm9yT2tBbmRIb2xkcygwKSk7Cn0KCnN0ZDo6YXRvbWljPGludD4gY291bnRlZF9zaWduYWxzOwoKdm9pZCBJbnRlcnZhbFRpbWVyQ291bnRpbmdTaWduYWxIYW5kbGVyKGludCBzaWcsIHNpZ2luZm9fdCogaW5mbywKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIHZvaWQqIHVjb250ZXh0KSB7CiAgY291bnRlZF9zaWduYWxzLmZldGNoX2FkZCgxICsgaW5mby0+c2lfb3ZlcnJ1bik7Cn0KClRFU1QoSW50ZXJ2YWxUaW1lclRlc3QsIFBlcmlvZGljR3JvdXBEaXJlY3RlZFNpZ25hbCkgewogIGNvbnN0ZXhwciBpbnQga1NpZ25vID0gU0lHVVNSMTsKICBjb25zdGV4cHIgaW50IGtTaWd2YWx1ZSA9IDQyOwoKICAvLyBJbnN0YWxsIG91ciBzaWduYWwgaGFuZGxlci4KICBjb3VudGVkX3NpZ25hbHMuc3RvcmUoMCk7CiAgc3RydWN0IHNpZ2FjdGlvbiBzYSA9IHt9OwogIHNhLnNhX3NpZ2FjdGlvbiA9IEludGVydmFsVGltZXJDb3VudGluZ1NpZ25hbEhhbmRsZXI7CiAgc2lnZW1wdHlzZXQoJnNhLnNhX21hc2spOwogIHNhLnNhX2ZsYWdzID0gU0FfU0lHSU5GTzsKICBjb25zdCBhdXRvIHNjb3BlZF9zaWdhY3Rpb24gPQogICAgICBBU1NFUlRfTk9fRVJSTk9fQU5EX1ZBTFVFKFNjb3BlZFNpZ2FjdGlvbihrU2lnbm8sIHNhKSk7CgogIC8vIEVuc3VyZSB0aGF0IGtTaWdubyBpcyB1bmJsb2NrZWQgb24gYXQgbGVhc3Qgb25lIHRocmVhZC4KICBjb25zdCBhdXRvIHNjb3BlZF9zaWdtYXNrID0KICAgICAgQVNTRVJUX05PX0VSUk5PX0FORF9WQUxVRShTY29wZWRTaWduYWxNYXNrKFNJR19VTkJMT0NLLCBrU2lnbm8pKTsKCiAgc3RydWN0IHNpZ2V2ZW50IHNldiA9IHt9OwogIHNldi5zaWdldl9ub3RpZnkgPSBTSUdFVl9TSUdOQUw7CiAgc2V2LnNpZ2V2X3NpZ25vID0ga1NpZ25vOwogIHNldi5zaWdldl92YWx1ZS5zaXZhbF9pbnQgPSBrU2lndmFsdWU7CiAgYXV0byB0aW1lciA9IEFTU0VSVF9OT19FUlJOT19BTkRfVkFMVUUoVGltZXJDcmVhdGUoQ0xPQ0tfTU9OT1RPTklDLCBzZXYpKTsKCiAgY29uc3RleHByIGFic2w6OkR1cmF0aW9uIGtQZXJpb2QgPSBhYnNsOjpTZWNvbmRzKDEpOwogIGNvbnN0ZXhwciBpbnQga0N5Y2xlcyA9IDM7CiAgc3RydWN0IGl0aW1lcnNwZWMgaXRzID0ge307CiAgaXRzLml0X3ZhbHVlID0gaXRzLml0X2ludGVydmFsID0gYWJzbDo6VG9UaW1lc3BlYyhrUGVyaW9kKTsKICBBU1NFUlRfTk9fRVJSTk8odGltZXIuU2V0KDAsIGl0cykpOwoKICBhYnNsOjpTbGVlcEZvcihrUGVyaW9kICoga0N5Y2xlcyArIGtUaW1lclNsYWNrKTsKICBFWFBFQ1RfR0UoY291bnRlZF9zaWduYWxzLmxvYWQoKSwga0N5Y2xlcyk7Cn0KClRFU1QoSW50ZXJ2YWxUaW1lclRlc3QsIFBlcmlvZGljVGhyZWFkRGlyZWN0ZWRTaWduYWwpIHsKICBjb25zdGV4cHIgaW50IGtTaWdubyA9IFNJR1VTUjE7CiAgY29uc3RleHByIGludCBrU2lndmFsdWUgPSA0MjsKCiAgLy8gQmxvY2sga1NpZ25vIHNvIHRoYXQgd2UgY2FuIGFjY3VtdWxhdGUgb3ZlcnJ1bnMuCiAgc2lnc2V0X3QgbWFzazsKICBzaWdlbXB0eXNldCgmbWFzayk7CiAgc2lnYWRkc2V0KCZtYXNrLCBrU2lnbm8pOwogIGNvbnN0IGF1dG8gc2NvcGVkX3NpZ21hc2sgPQogICAgICBBU1NFUlRfTk9fRVJSTk9fQU5EX1ZBTFVFKFNjb3BlZFNpZ25hbE1hc2soU0lHX0JMT0NLLCBtYXNrKSk7CgogIHN0cnVjdCBzaWdldmVudCBzZXYgPSB7fTsKICBzZXYuc2lnZXZfbm90aWZ5ID0gU0lHRVZfVEhSRUFEX0lEOwogIHNldi5zaWdldl9zaWdubyA9IGtTaWdubzsKICBzZXYuc2lnZXZfdmFsdWUuc2l2YWxfaW50ID0ga1NpZ3ZhbHVlOwogIHNldi5zaWdldl9ub3RpZnlfdGhyZWFkX2lkID0gZ2V0dGlkKCk7CiAgYXV0byB0aW1lciA9IEFTU0VSVF9OT19FUlJOT19BTkRfVkFMVUUoVGltZXJDcmVhdGUoQ0xPQ0tfTU9OT1RPTklDLCBzZXYpKTsKCiAgY29uc3RleHByIGFic2w6OkR1cmF0aW9uIGtQZXJpb2QgPSBhYnNsOjpTZWNvbmRzKDEpOwogIGNvbnN0ZXhwciBpbnQga0N5Y2xlcyA9IDM7CiAgc3RydWN0IGl0aW1lcnNwZWMgaXRzID0ge307CiAgaXRzLml0X3ZhbHVlID0gaXRzLml0X2ludGVydmFsID0gYWJzbDo6VG9UaW1lc3BlYyhrUGVyaW9kKTsKICBBU1NFUlRfTk9fRVJSTk8odGltZXIuU2V0KDAsIGl0cykpOwogIGFic2w6OlNsZWVwRm9yKGtQZXJpb2QgKiBrQ3ljbGVzICsga1RpbWVyU2xhY2spOwoKICAvLyBBdCBsZWFzdCBrQ3ljbGVzIGV4cGlyYXRpb25zIHNob3VsZCBoYXZlIG9jY3VycmVkLCByZXN1bHRpbmcgaW4ga0N5Y2xlcy0xCiAgLy8gb3ZlcnJ1bnMgKHRoZSBmaXJzdCBleHBpcmF0aW9uIHNlbnQgdGhlIHNpZ25hbCBzdWNjZXNzZnVsbHkpLgogIHNpZ2luZm9fdCBzaTsKICBzdHJ1Y3QgdGltZXNwZWMgemVyb190cyA9IGFic2w6OlRvVGltZXNwZWMoYWJzbDo6WmVyb0R1cmF0aW9uKCkpOwogIEFTU0VSVF9USEFUKHNpZ3RpbWVkd2FpdCgmbWFzaywgJnNpLCAmemVyb190cyksCiAgICAgICAgICAgICAgU3lzY2FsbFN1Y2NlZWRzV2l0aFZhbHVlKGtTaWdubykpOwogIEVYUEVDVF9FUShzaS5zaV9zaWdubywga1NpZ25vKTsKICBFWFBFQ1RfRVEoc2kuc2lfY29kZSwgU0lfVElNRVIpOwogIEVYUEVDVF9FUShzaS5zaV90aW1lcmlkLCB0aW1lci5nZXQoKSk7CiAgRVhQRUNUX0dFKHNpLnNpX292ZXJydW4sIGtDeWNsZXMgLSAxKTsKICBFWFBFQ1RfRVEoc2kuc2lfaW50LCBrU2lndmFsdWUpOwoKICAvLyBLaWxsIHRoZSB0aW1lciwgdGhlbiBkcmFpbiBhbnkgYWRkaXRpb25hbCBzaWduYWwgaXQgbWF5IGhhdmUgZW5xdWV1ZWQuIFdlCiAgLy8gY2FuJ3QgZG8gdGhpcyBiZWZvcmUgdGhlIHByZWNlZGluZyBzaWd0aW1lZHdhaXQgYmVjYXVzZSBzdG9wcGluZyBvcgogIC8vIGRlbGV0aW5nIHRoZSB0aW1lciByZXNldHMgc2lfb3ZlcnJ1biB0byAwLgogIHRpbWVyLnJlc2V0KCk7CiAgc2lndGltZWR3YWl0KCZtYXNrLCAmc2ksICZ6ZXJvX3RzKTsKfQoKVEVTVChJbnRlcnZhbFRpbWVyVGVzdCwgT3RoZXJUaHJlYWRHcm91cCkgewogIGNvbnN0ZXhwciBpbnQga1NpZ25vID0gU0lHVVNSMTsKCiAgLy8gQ3JlYXRlIGEgc3VicHJvY2VzcyB0aGF0IGRvZXMgbm90aGluZyB1bnRpbCBraWxsZWQuCiAgcGlkX3QgY2hpbGRfcGlkOwogIGNvbnN0IGF1dG8gc3AgPSBBU1NFUlRfTk9fRVJSTk9fQU5EX1ZBTFVFKEZvcmtBbmRFeGVjKAogICAgICAiL3Byb2Mvc2VsZi9leGUiLCBFeGVjdmVBcnJheSh7InRpbWVycyIsICItLXRpbWVyc190ZXN0X3NsZWVwIn0pLAogICAgICBFeGVjdmVBcnJheSgpLCAmY2hpbGRfcGlkLCBudWxscHRyKSk7CgogIC8vIFZlcmlmeSB0aGF0IHdlIGNhbid0IGNyZWF0ZSBhIHRpbWVyIHRoYXQgd291bGQgc2VuZCBzaWduYWxzIHRvIGl0LgogIHN0cnVjdCBzaWdldmVudCBzZXYgPSB7fTsKICBzZXYuc2lnZXZfbm90aWZ5ID0gU0lHRVZfVEhSRUFEX0lEOwogIHNldi5zaWdldl9zaWdubyA9IGtTaWdubzsKICBzZXYuc2lnZXZfbm90aWZ5X3RocmVhZF9pZCA9IGNoaWxkX3BpZDsKICBFWFBFQ1RfVEhBVChUaW1lckNyZWF0ZShDTE9DS19NT05PVE9OSUMsIHNldiksIFBvc2l4RXJyb3JJcyhFSU5WQUwsIF8pKTsKfQoKVEVTVChJbnRlcnZhbFRpbWVyVGVzdCwgUmVhbFRpbWVTaWduYWxzQXJlTm90RHVwbGljYXRlZCkgewogIGNvbnN0IGludCBrU2lnbm8gPSBTSUdSVE1JTjsKICBjb25zdGV4cHIgaW50IGtTaWd2YWx1ZSA9IDQyOwoKICAvLyBCbG9jayBzaWdubyBzbyB0aGF0IHdlIGNhbiBhY2N1bXVsYXRlIG92ZXJydW5zLgogIHNpZ3NldF90IG1hc2s7CiAgc2lnZW1wdHlzZXQoJm1hc2spOwogIHNpZ2FkZHNldCgmbWFzaywga1NpZ25vKTsKICBjb25zdCBhdXRvIHNjb3BlZF9zaWdtYXNrID0gU2NvcGVkU2lnbmFsTWFzayhTSUdfQkxPQ0ssIG1hc2spOwoKICBzdHJ1Y3Qgc2lnZXZlbnQgc2V2ID0ge307CiAgc2V2LnNpZ2V2X25vdGlmeSA9IFNJR0VWX1RIUkVBRF9JRDsKICBzZXYuc2lnZXZfc2lnbm8gPSBrU2lnbm87CiAgc2V2LnNpZ2V2X3ZhbHVlLnNpdmFsX2ludCA9IGtTaWd2YWx1ZTsKICBzZXYuc2lnZXZfbm90aWZ5X3RocmVhZF9pZCA9IGdldHRpZCgpOwogIGNvbnN0IGF1dG8gdGltZXIgPQogICAgICBBU1NFUlRfTk9fRVJSTk9fQU5EX1ZBTFVFKFRpbWVyQ3JlYXRlKENMT0NLX01PTk9UT05JQywgc2V2KSk7CgogIC8vIERpc2FibGUgc2F2ZSBiZWNhdXNlIGEgc2F2ZS9yZXN0b3JlIGN5Y2xlIGFkZHMgY29uc2lkZXJhYmxlIHRpbWUgb3ZlcmhlYWQKICAvLyBiZXR3ZWVuIGVhY2ggc3lzY2FsbCBzdWNoIHRoYXQgYHRpbWVyYCBmaXJlcyBpbiBiZXR3ZWVuIHNpZ3RpbWVkd2FpdCgpIGNhbGwKICAvLyBhbmQgdGltZXIuU2V0KCkgY2FsbCwgd2hpY2ggY2F1c2VzIHRoZSBsYXN0IHNpZ3RpbWVkd2FpdCgpIGNoZWNrIHRvIGZhaWwuCiAgRGlzYWJsZVNhdmUgZHM7CgogIGNvbnN0ZXhwciBhYnNsOjpEdXJhdGlvbiBrUGVyaW9kID0gYWJzbDo6U2Vjb25kcygxKTsKICBjb25zdGV4cHIgaW50IGtDeWNsZXMgPSAzOwogIHN0cnVjdCBpdGltZXJzcGVjIGl0cyA9IHt9OwogIGl0cy5pdF92YWx1ZSA9IGl0cy5pdF9pbnRlcnZhbCA9IGFic2w6OlRvVGltZXNwZWMoa1BlcmlvZCk7CiAgQVNTRVJUX05PX0VSUk5PKHRpbWVyLlNldCgwLCBpdHMpKTsKICBhYnNsOjpTbGVlcEZvcihrUGVyaW9kICoga0N5Y2xlcyArIGtUaW1lclNsYWNrKTsKCiAgc3RydWN0IHRpbWVzcGVjIHplcm9fdHMgPSBhYnNsOjpUb1RpbWVzcGVjKGFic2w6Olplcm9EdXJhdGlvbigpKTsKICBzaWdpbmZvX3Qgc2k7CiAgQVNTRVJUX1RIQVQoc2lndGltZWR3YWl0KCZtYXNrLCAmc2ksICZ6ZXJvX3RzKSwKICAgICAgICAgICAgICBTeXNjYWxsU3VjY2VlZHNXaXRoVmFsdWUoa1NpZ25vKSk7CiAgRVhQRUNUX0VRKHNpLnNpX3NpZ25vLCBrU2lnbm8pOwogIEVYUEVDVF9FUShzaS5zaV9jb2RlLCBTSV9USU1FUik7CiAgRVhQRUNUX0VRKHNpLnNpX3RpbWVyaWQsIHRpbWVyLmdldCgpKTsKICBFWFBFQ1RfRVEoc2kuc2lfb3ZlcnJ1biwga0N5Y2xlcyAtIDEpOwogIEVYUEVDVF9FUShzaS5zaV9pbnQsIGtTaWd2YWx1ZSk7CgogIC8vIFN0b3AgdGhlIHRpbWVyIHNvIHRoYXQgbm8gZnVydGhlciBzaWduYWxzIGFyZSBlbnF1ZXVlZCBhZnRlciBzaWd0aW1lZHdhaXQuCiAgaXRzLml0X3ZhbHVlID0gaXRzLml0X2ludGVydmFsID0gemVyb190czsKICBBU1NFUlRfTk9fRVJSTk8odGltZXIuU2V0KDAsIGl0cykpOwoKICAvLyBUaGUgdGltZXIgc2hvdWxkIGhhdmUgc2VudCBvbmx5IGEgc2luZ2xlIHNpZ25hbCwgZXZlbiB0aG91Z2ggdGhlIGtlcm5lbAogIC8vIHN1cHBvcnRzIGVucXVldWVpbmcgb2YgbXVsdGlwbGUgUlQgc2lnbmFscy4KICBFWFBFQ1RfVEhBVChzaWd0aW1lZHdhaXQoJm1hc2ssICZzaSwgJnplcm9fdHMpLAogICAgICAgICAgICAgIFN5c2NhbGxGYWlsc1dpdGhFcnJubyhFQUdBSU4pKTsKfQoKVEVTVChJbnRlcnZhbFRpbWVyVGVzdCwgQWxyZWFkeVBlbmRpbmdTaWduYWwpIHsKICBjb25zdGV4cHIgaW50IGtTaWdubyA9IFNJR1VTUjE7CiAgY29uc3RleHByIGludCBrU2lndmFsdWUgPSA0MjsKCiAgLy8gQmxvY2sga1NpZ25vIHNvIHRoYXQgd2UgY2FuIGFjY3VtdWxhdGUgb3ZlcnJ1bnMuCiAgc2lnc2V0X3QgbWFzazsKICBzaWdlbXB0eXNldCgmbWFzayk7CiAgc2lnYWRkc2V0KCZtYXNrLCBrU2lnbm8pOwogIGNvbnN0IGF1dG8gc2NvcGVkX3NpZ21hc2sgPQogICAgICBBU1NFUlRfTk9fRVJSTk9fQU5EX1ZBTFVFKFNjb3BlZFNpZ25hbE1hc2soU0lHX0JMT0NLLCBtYXNrKSk7CgogIC8vIFNlbmQgb3Vyc2VsdmVzIGEgc2lnbmFsLCBwcmV2ZW50aW5nIHRoZSB0aW1lciBmcm9tIGVucXVldWluZy4KICBBU1NFUlRfVEhBVCh0Z2tpbGwoZ2V0cGlkKCksIGdldHRpZCgpLCBrU2lnbm8pLCBTeXNjYWxsU3VjY2VlZHMoKSk7CgogIHN0cnVjdCBzaWdldmVudCBzZXYgPSB7fTsKICBzZXYuc2lnZXZfbm90aWZ5ID0gU0lHRVZfVEhSRUFEX0lEOwogIHNldi5zaWdldl9zaWdubyA9IGtTaWdubzsKICBzZXYuc2lnZXZfdmFsdWUuc2l2YWxfaW50ID0ga1NpZ3ZhbHVlOwogIHNldi5zaWdldl9ub3RpZnlfdGhyZWFkX2lkID0gZ2V0dGlkKCk7CiAgYXV0byB0aW1lciA9IEFTU0VSVF9OT19FUlJOT19BTkRfVkFMVUUoVGltZXJDcmVhdGUoQ0xPQ0tfTU9OT1RPTklDLCBzZXYpKTsKCiAgY29uc3RleHByIGFic2w6OkR1cmF0aW9uIGtQZXJpb2QgPSBhYnNsOjpTZWNvbmRzKDEpOwogIGNvbnN0ZXhwciBpbnQga0N5Y2xlcyA9IDM7CiAgc3RydWN0IGl0aW1lcnNwZWMgaXRzID0ge307CiAgaXRzLml0X3ZhbHVlID0gaXRzLml0X2ludGVydmFsID0gYWJzbDo6VG9UaW1lc3BlYyhrUGVyaW9kKTsKICBBU1NFUlRfTk9fRVJSTk8odGltZXIuU2V0KDAsIGl0cykpOwoKICAvLyBFbmQgdGhlIHNsZWVwIG9uZSBjeWNsZSBzaG9ydDsgd2Ugd2lsbCBzbGVlcCBmb3Igb25lIG1vcmUgY3ljbGUgYmVsb3cuCiAgYWJzbDo6U2xlZXBGb3Ioa1BlcmlvZCAqIChrQ3ljbGVzIC0gMSkpOwoKICAvLyBEZXF1ZXVlIHRoZSBmaXJzdCBzaWduYWwsIHdoaWNoIHdlIHNlbnQgdG8gb3Vyc2VsdmVzIHdpdGggdGdraWxsLgogIHNpZ2luZm9fdCBzaTsKICBzdHJ1Y3QgdGltZXNwZWMgemVyb190cyA9IGFic2w6OlRvVGltZXNwZWMoYWJzbDo6WmVyb0R1cmF0aW9uKCkpOwogIEFTU0VSVF9USEFUKHNpZ3RpbWVkd2FpdCgmbWFzaywgJnNpLCAmemVyb190cyksCiAgICAgICAgICAgICAgU3lzY2FsbFN1Y2NlZWRzV2l0aFZhbHVlKGtTaWdubykpOwogIEVYUEVDVF9FUShzaS5zaV9zaWdubywga1NpZ25vKTsKICAvLyBnbGliYyBzaWd0aW1lZHdhaXQgc2lsZW50bHkgcmVwbGFjZXMgU0lfVEtJTEwgd2l0aCBTSV9VU0VSOgogIC8vIHN5c2RlcHMvdW5peC9zeXN2L2xpbnV4L3NpZ3RpbWVkd2FpdC5jOl9fc2lndGltZWR3YWl0KCkuIFRoaXMgaXNuJ3QKICAvLyBkb2N1bWVudGVkLCBzbyB3ZSBkb24ndCBkZXBlbmQgb24gaXQuCiAgRVhQRUNUX1RIQVQoc2kuc2lfY29kZSwgQW55T2YoU0lfVVNFUiwgU0lfVEtJTEwpKTsKCiAgLy8gU2xlZXAgZm9yIDEgbW9yZSBjeWNsZSB0byBnaXZlIHRoZSB0aW1lciB0aW1lIHRvIHNlbmQgYSBzaWduYWwuCiAgYWJzbDo6U2xlZXBGb3Ioa1BlcmlvZCArIGtUaW1lclNsYWNrKTsKCiAgLy8gQXQgbGVhc3Qga0N5Y2xlcyBleHBpcmF0aW9ucyBzaG91bGQgaGF2ZSBvY2N1cnJlZCwgcmVzdWx0aW5nIGluIGtDeWNsZXMtMQogIC8vIG92ZXJydW5zICh0aGUgbGFzdCBleHBpcmF0aW9uIHNlbnQgdGhlIHNpZ25hbCBzdWNjZXNzZnVsbHkpLgogIEFTU0VSVF9USEFUKHNpZ3RpbWVkd2FpdCgmbWFzaywgJnNpLCAmemVyb190cyksCiAgICAgICAgICAgICAgU3lzY2FsbFN1Y2NlZWRzV2l0aFZhbHVlKGtTaWdubykpOwogIEVYUEVDVF9FUShzaS5zaV9zaWdubywga1NpZ25vKTsKICBFWFBFQ1RfRVEoc2kuc2lfY29kZSwgU0lfVElNRVIpOwogIEVYUEVDVF9FUShzaS5zaV90aW1lcmlkLCB0aW1lci5nZXQoKSk7CiAgRVhQRUNUX0dFKHNpLnNpX292ZXJydW4sIGtDeWNsZXMgLSAxKTsKICBFWFBFQ1RfRVEoc2kuc2lfaW50LCBrU2lndmFsdWUpOwoKICAvLyBLaWxsIHRoZSB0aW1lciwgdGhlbiBkcmFpbiBhbnkgYWRkaXRpb25hbCBzaWduYWwgaXQgbWF5IGhhdmUgZW5xdWV1ZWQuIFdlCiAgLy8gY2FuJ3QgZG8gdGhpcyBiZWZvcmUgdGhlIHByZWNlZGluZyBzaWd0aW1lZHdhaXQgYmVjYXVzZSBzdG9wcGluZyBvcgogIC8vIGRlbGV0aW5nIHRoZSB0aW1lciByZXNldHMgc2lfb3ZlcnJ1biB0byAwLgogIHRpbWVyLnJlc2V0KCk7CiAgc2lndGltZWR3YWl0KCZtYXNrLCAmc2ksICZ6ZXJvX3RzKTsKfQoKVEVTVChJbnRlcnZhbFRpbWVyVGVzdCwgTmVnYXRpdmVJbnRlcnZhbCkgewogIHN0cnVjdCBzaWdldmVudCBzZXYgPSB7fTsKICBzZXYuc2lnZXZfbm90aWZ5ID0gU0lHRVZfU0lHTkFMOwogIHNldi5zaWdldl9zaWdubyA9IFNJR0FMUk07CiAgYXV0byB0aW1lciA9CiAgICAgIEFTU0VSVF9OT19FUlJOT19BTkRfVkFMVUUoVGltZXJDcmVhdGUoQ0xPQ0tfUFJPQ0VTU19DUFVUSU1FX0lELCBzZXYpKTsKICBzdHJ1Y3QgaXRpbWVyc3BlYyBuZXdfdmFsdWUgPSB7fTsKICBuZXdfdmFsdWUuaXRfaW50ZXJ2YWwudHZfc2VjID0gMDsKICBuZXdfdmFsdWUuaXRfaW50ZXJ2YWwudHZfbnNlYyA9IC0yOyAgLy8gTmVnYXRpdmUuCiAgbmV3X3ZhbHVlLml0X3ZhbHVlLnR2X3NlYyA9IDA7CiAgbmV3X3ZhbHVlLml0X3ZhbHVlLnR2X25zZWMgPSAxMDAwMDAwOwogIC8vIE1ha2Ugc3VyZSB0aGlzIGZhaWxzIHdpdGggRUlOVkFMLgogIEVYUEVDVF9USEFUKHRpbWVyLlNldCgwLCBuZXdfdmFsdWUpLCBQb3NpeEVycm9ySXMoRUlOVkFMLCBfKSk7Cn0KCn0gIC8vIG5hbWVzcGFjZQp9ICAvLyBuYW1lc3BhY2UgdGVzdGluZwp9ICAvLyBuYW1lc3BhY2UgZ3Zpc29yCgppbnQgbWFpbihpbnQgYXJnYywgY2hhcioqIGFyZ3YpIHsKICBndmlzb3I6OnRlc3Rpbmc6OlRlc3RJbml0KCZhcmdjLCAmYXJndik7CgogIGlmIChhYnNsOjpHZXRGbGFnKEZMQUdTX3RpbWVyc190ZXN0X3NsZWVwKSkgewogICAgd2hpbGUgKHRydWUpIHsKICAgICAgYWJzbDo6U2xlZXBGb3IoYWJzbDo6U2Vjb25kcygxMCkpOwogICAgfQogIH0KCiAgcmV0dXJuIGd2aXNvcjo6dGVzdGluZzo6UnVuQWxsVGVzdHMoKTsKfQo=', validate=True)
assert hashlib.sha256(value).hexdigest() == '99e87c3cf3973545281cb476062975d990cb1287f28855a297b52284d1c77409'
path.write_bytes(value)
PYTIMER_BEFORE
  cp test/syscalls/linux/timers.cc "$artifacts/timers.before.cc"
  git diff -- test/syscalls/linux/timers.cc > "$artifacts/before-source.patch"
  timer_phase before "$artifacts/before-targets" \
    --test_filter=TimerTest.RlimitCpuInheritedAcrossFork
  cp "$selection_dir/timers.after.cc" test/syscalls/linux/timers.cc
  cmp "$selection_dir/timers.after.cc" test/syscalls/linux/timers.cc
  git diff --exit-code -- test/syscalls/linux/timers.cc
  timer_phase after "$artifacts/after-targets"
  cmp "$selection_dir/timers.after.cc" test/syscalls/linux/timers.cc
  git diff --exit-code -- test/syscalls/linux/timers.cc
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
