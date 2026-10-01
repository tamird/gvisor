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

lanes=(build-all plugin-build nogo unit unit-v1 container container-v1 smoke smoke-race release-artifacts release-repository cpu-images gpu-images docker docker-v1 overlay swgso hostnet plugin-network 'do' root portforward posture startup benchmarks containerd bwrap fsstress packetimpact iptables nftables packetdrill language-directfs language-goferfs kubernetes podman syzkaller website go-export workflows lint lint-cc governance license-check license-headers python-distributions syscalls syscalls-save syscalls-resume)

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
The all architecture selection combines unit, release-repository and syscalls
with the target-configured test lanes described in test/rbe/README.md.
Only unit and syscalls add ARM64 variants.
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
  if [[ $arch == all ]]; then
    case "$lane" in
      nogo|unit|release-artifacts|release-repository|python-distributions|website|syscalls|syscalls-save|syscalls-resume|smoke|smoke-race|plugin-build|plugin-network|do|docker|root|portforward|bwrap|workflows|language-directfs|language-goferfs|overlay|swgso|hostnet|containerd|fsstress|packetimpact|iptables|nftables|packetdrill|kubernetes|podman|syzkaller|go-export|cpu-images|gpu-images|posture|startup|benchmarks) ;;
      *) printf 'Lane %s does not support the all architecture selection.\n' "$lane" >&2; exit 2 ;;
    esac
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

# Set the caller's targets array from the same owning suites for standalone and
# combined invocations.
shared_test_targets() {
  local target_arch=$2
  case "$1" in
    do) targets=(//:do_tests) ;;
    docker) targets=(//test/docker:owned_tests) ;;
    plugin-network) targets=(//test/docker:plugin_network_tests) ;;
    root) targets=(//test/root:root_test_owned) ;;
    posture) targets=(//test/root:sandbox_posture_test_owned) ;;
    startup) targets=(//test/benchmarks/base:startup_test_owned) ;;
    benchmarks) targets=(//test/benchmarks:continuous_tests) ;;
    portforward) targets=(//test/root:portforward_test_owned) ;;
    bwrap) targets=(//runsc/cmd/alias/bwrap:bwrap_integration_test) ;;
    workflows) targets=(//:github_actions_test //:github_workflows_test //:buildkite_pipelines_test) ;;
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

# A file keeps large ordered target lists below Linux's argument-size limit.
# Aquery ignores target_pattern_file; apply this config after the caller's options.
analyze_profile() {
  local patterns=$1 events=$2
  shift 2
  local rc=$events.bazelrc
  python3 test/rbe/unit_matrix.py universe-rc "$patterns" > "$rc"
  bazel "--bazelrc=$rc" aquery "$@" --config=rbe-selection \
    "--build_event_json_file=$events" 'set()'
}

# Let Bazel select each public syscall profile before checking worker capacity.
# Aquery inherits build options, so the canonical loading filters live there;
# test inherits the same options. Its ordered universe comes from Make's roots.
select_syscall_profile() {
  local selection_dir=$1 lane=$2 syscall_arch=$3 target_config=x86_64 prefix
  local -a profile_options=()
  prefix=$selection_dir/$lane-$syscall_arch
  if [[ $syscall_arch == arm64 ]]; then
    target_config=aarch64
  fi
  case "$lane" in
    syscalls) profile_options=("--config=syscalls-$syscall_arch") ;;
    syscalls-save) profile_options=(--test_tag_filters=save_restore) ;;
    syscalls-resume) profile_options=(--test_tag_filters=save_resume) ;;
    *) printf 'Unknown syscall profile: %s\n' "$lane" >&2; return 2 ;;
  esac
  analyze_profile test/syscalls.targets "$prefix-profile.json" \
    --config=rbe-matrix "--config=$target_config" \
    "${profile_options[@]}" --build_tests_only
  python3 test/rbe/unit_matrix.py profile-actions \
    "$prefix-profile.json" "$syscall_arch" > "$prefix.query"
  bazel aquery --config=rbe-matrix --config=x86_64 --build_tests_only \
    --output=jsonproto --include_artifacts=false \
    "--build_event_json_file=$prefix-routing.json" \
    "--query_file=$prefix.query" > "$prefix-actions.json"
  python3 test/rbe/unit_matrix.py select-syscalls \
    "$prefix-profile.json" "$syscall_arch" "$prefix-routing.json" \
    "$prefix-actions.json" "$prefix-targets"
}

# Preserve the canonical unit roots, including filtered tests' build-only work.
# Append other profiles' selected owners, checking compatibility with the final
# filters. Release artifacts already select both CPUs in their owning graph.
run_platform_matrix() (
  set -e
  local selection_dir lane command=build include_unit=false include_syscalls=false include_checkpoints=false include_nogo=false
  local -a options=() targets=() verification_options=()
  selection_dir=$(mktemp -d)
  trap 'rm -rf "$selection_dir"' EXIT
  : > "$selection_dir/targets"
  : > "$selection_dir/explicit-targets"
  : > "$selection_dir/shared-targets"
  : > "$selection_dir/filtered-targets"
  for lane in "$@"; do
    case "$lane" in
      plugin-build|release-artifacts|python-distributions|website) ;;
      *) command='test' ;;
    esac
    case "$lane" in
      unit) include_unit=true ;;
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
        python3 test/rbe/unit_matrix.py select test/unit.targets "$selection_dir/owners" \
          "$selection_dir/actions.json" "$selection_dir/unit-targets"
        cat "$selection_dir/unit-targets" >> "$selection_dir/targets"
        options+=(--config=unit --strip=never)
        if [[ $include_nogo == true ]]; then
          # Preserve the original unit selection before allowing Nogo through
          # its wildcard roots. Verify the complete union before execution.
          analyze_profile "$selection_dir/unit-targets" "$selection_dir/unit-profile.json" \
            --config=rbe-matrix --config=x86_64 --config=unit --strip=never --build_tests_only
          verification_options+=(--profile "$selection_dir/unit-profile.json")
        fi
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
        if [[ $include_unit == true || $include_checkpoints == true ]]; then
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
      smoke|smoke-race)
        printf '//:release_%s_test\n' "${lane//-/_}" >> "$selection_dir/explicit-targets"
        options+=(--strip=never)
        ;;
      posture|startup|benchmarks)
        shared_test_targets "$lane" amd64
        printf '%s\n' "${targets[@]}" >> "$selection_dir/filtered-targets"
        printf 'Combined lane %s retains AMD64 execution; no ARM64 coverage is added.\n' "$lane"
        options+=(--strip=never)
        ;;
      plugin-build)
        # This is a build-only root, not an executable test for the loading
        # verifier. Its own transition preserves opt/strip=sometimes.
        printf '%s\n' '//runsc:runsc-plugin-stack-build' >> "$selection_dir/targets"
        ;;
      plugin-network|do|docker|root|portforward|bwrap|workflows|language-directfs|language-goferfs|overlay|swgso|hostnet|containerd|fsstress|packetimpact|iptables|nftables|packetdrill|kubernetes|podman|syzkaller|go-export|cpu-images|gpu-images)
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
  if [[ $include_nogo == true ]]; then
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
    if [[ $include_nogo == true || $include_syscalls == true || -s $selection_dir/filtered-targets || ( $include_unit == true && -s $selection_dir/shared-targets ) ]]; then
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
      targets=(//runsc:runsc-plugin-stack-build)
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
    do|docker|root|portforward|bwrap|workflows|overlay|swgso|hostnet|containerd|fsstress|packetimpact|iptables|nftables|packetdrill|podman|cpu-images|gpu-images)
      if [[ $lane == "do" && $arch != amd64 ]]; then
        printf 'The public do smoke checks are declared for AMD64.\n' >&2
        return 2
      fi
      shared_test_targets "$lane" "$arch"
      ;;
    posture)
      options=(--test_tag_filters=-requires-kvm)
      shared_test_targets "$lane" "$arch"
      ;;
    startup)
      options=(--test_tag_filters=-requires-kvm)
      shared_test_targets "$lane" "$arch"
      ;;
    benchmarks)
      if [[ $arch != amd64 ]]; then
        printf 'Continuous CI benchmarks are declared for AMD64; ARM64 workers remain unqualified.\n' >&2
        return 2
      fi
      # CI reports these jobs as soft failures. Keep their status visible here;
      # --keep_going still collects the other complete benchmark workloads.
      options=(--test_tag_filters=-requires-kvm)
      shared_test_targets "$lane" "$arch"
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
