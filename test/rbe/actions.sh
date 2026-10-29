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

# Run on a Linux coordinator; GitHub supplies these inputs as data.
set -euo pipefail
[[ $(uname -s) == Linux ]]
[[ $(git rev-parse HEAD) == "$QUALIFICATION_COMMIT" ]]

for argument in "$@"; do
  if [[ $argument != --bazel-option=--?* ]]; then
    printf 'Expected --bazel-option=--FLAG, got: %s\n' "$argument" >&2
    exit 2
  fi
done

# read consumes one line. Reject extra lines rather than losing selected lanes.
if [[ $QUALIFICATION_LANES == *$'\n'* ]]; then
  printf 'Supply qualification lanes on one line.\n' >&2
  exit 2
fi
read -r -a lanes <<< "$QUALIFICATION_LANES"
if (( ${#lanes[@]} == 0 )); then
  printf 'Select at least one qualification lane.\n' >&2
  exit 2
fi
for lane in "${lanes[@]}"; do
  # Options such as --list could otherwise succeed without running any lane.
  if [[ ! $lane =~ ^[a-z][a-z0-9-]*$ ]]; then
    printf 'Invalid lane name: %s\n' "$lane" >&2
    exit 2
  fi
done
options=("--arch=$QUALIFICATION_ARCH" "--clock-source=${QUALIFICATION_CLOCK_SOURCE:-calibrated}")
if [[ -n ${QUALIFICATION_SYSCALL_BUCKET:-} ]]; then
  options+=("--syscall-bucket=$QUALIFICATION_SYSCALL_BUCKET")
fi
if [[ -n ${QUALIFICATION_TEST_FILTER:-} ]]; then
  options+=("--test-filter=$QUALIFICATION_TEST_FILTER")
fi
if [[ -n ${QUALIFICATION_TARGETS:-} ]]; then
  if [[ $QUALIFICATION_TARGETS == *$'\n'* ]]; then
    printf 'Supply target labels on one line.\n' >&2
    exit 2
  fi
  read -r -a selected_targets <<< "$QUALIFICATION_TARGETS"
  if (( ${#selected_targets[@]} == 0 )); then
    printf 'Supply at least one explicit target label.\n' >&2
    exit 2
  fi
  case "${lanes[*]}" in
    build|test)
      for target in "${selected_targets[@]}"; do
        options+=("--${lanes[0]}-target=$target")
      done
      ;;
    syscalls|syscalls-save|syscalls-kvm|syscalls-rc|pgo|docker|cpu-images)
      for target in "${selected_targets[@]}"; do
        options+=("--test-target=$target")
      done
      ;;
    benchmarks)
      for target in "${selected_targets[@]}"; do
        options+=("--benchmark-target=$target")
      done
      ;;
    *)
      printf 'Explicit targets require the build, test, syscall, KVM syscall, RC syscall, Docker, CPU image or benchmarks lane.\n' >&2
      exit 2
      ;;
  esac
fi
temporary_files=()
GVISOR_DOCKER_NETWORK=
qualification_private=
qualification_workspace=$PWD
qualification_artifacts=
cleanup() {
  local status=$? capture_status=0 bundle_status=0
  trap - EXIT
  # Preserve dependency resolution output even on failure or interruption.
  # A dirty lock remains a source-integrity failure; it is never restored here.
  if [[ -n $qualification_artifacts ]]; then
    cp -- "$qualification_workspace/MODULE.bazel.lock" "$qualification_artifacts/MODULE.bazel.lock" || capture_status=1
    sha256sum "$qualification_artifacts/MODULE.bazel.lock" > "$qualification_artifacts/MODULE.bazel.lock.sha256" || capture_status=1
    git -C "$qualification_workspace" diff --binary --full-index HEAD -- MODULE.bazel.lock > "$qualification_artifacts/MODULE.bazel.lock.diff" || capture_status=1
    cmp -- "$qualification_artifacts/MODULE.bazel.lock.before" "$qualification_artifacts/MODULE.bazel.lock" || capture_status=1
    git -C "$qualification_workspace" rev-parse HEAD > "$qualification_artifacts/final-head.txt" || capture_status=1
    [[ $(cat "$qualification_artifacts/final-head.txt") == "$QUALIFICATION_COMMIT" ]] || capture_status=1
    git -C "$qualification_workspace" status --porcelain --untracked-files=no > "$qualification_artifacts/source-after.txt" || capture_status=1
    [[ ! -s "$qualification_artifacts/source-after.txt" ]] || capture_status=1
    printf '%s\n' "$capture_status" > "$qualification_artifacts/lock-capture-exit" || capture_status=1
  fi
  if ! rm -f -- "${temporary_files[@]}"; then
    capture_status=1
  fi
  if [[ -n $qualification_private ]] && ! rm -rf -- "$qualification_private"; then
    capture_status=1
  fi
  if [[ -n $GVISOR_DOCKER_NETWORK ]]; then
    # Bazel has removed its --rm test containers before returning. An endpoint
    # left behind is a cleanup failure, not a reason to force-disconnect it.
    if ! sudo -n docker --host=unix:///var/run/docker.sock network rm "$GVISOR_DOCKER_NETWORK"; then
      capture_status=1
    fi
  fi
  if [[ -n $qualification_artifacts ]]; then
    if [[ ${qualification_source_images:-false} == true ]]; then
      # Source provenance is a separate audit, not a rewrite of the workload's
      # canonical exit. Missing/failed audits make matrix composition incomplete.
      local audit_status=0
      timeout --signal=TERM --kill-after=5s 600s \
        python3 "$qualification_workspace/test/rbe/qualification_report.py" source-image-row \
        --directory "$qualification_artifacts" --decoder "${QUALIFICATION_DECODER:-}" || audit_status=$?
      printf '%s\n' "$audit_status" > "$qualification_artifacts/source-image-audit-exit.txt"
    fi
    # Reconcile on the coordinator and upload one archive. Preserve the original
    # command failure if reporting or final cleanup also fails.
    timeout --signal=TERM --kill-after=5s 600s python3 "$qualification_workspace/test/rbe/qualification_report.py" bundle \
      --directory "$qualification_artifacts" --runner-exit "$status" --capture-exit "$capture_status" \
      --output "${BUILDBUDDY_ARTIFACTS_DIRECTORY:-$RUNNER_TEMP}/qualification.tar.gz" || bundle_status=$?
    if [[ -n ${BUILDBUDDY_ARTIFACTS_DIRECTORY:-} ]]; then
      # A slow log download must not delay reporting the completed test results.
      gzip -c -- "$qualification_artifacts/verdict.json" > "$BUILDBUDDY_ARTIFACTS_DIRECTORY/verdict.json.gz" || bundle_status=$?
    fi
  fi
  if (( status == 0 )); then
    if (( capture_status != 0 )); then status=$capture_status; else status=$bundle_status; fi
  fi
  exit "$status"
}
trap cleanup EXIT

# Both coordinator types retain and reconcile reports before uploading one
# bundle. Bazel downloads the selected reports directly from its remote cache.
RUNNER_TEMP=${RUNNER_TEMP:-$(mktemp -d)}
export RUNNER_TEMP
mkdir -p "$RUNNER_TEMP/qualification"
qualification_artifacts=$RUNNER_TEMP/qualification
qualification_private=$(mktemp -d "$RUNNER_TEMP/qualification-private.XXXXXX")
# BuildBuddy reuses the last startup options for its final workspace-lock check.
# Its coordinator must still be able to read this file after our cleanup.
qualification_rc=$qualification_workspace/test/rbe/qualification.bazelrc
qualification_bazel_binary=
qualification_root_bazel=false
export qualification_workspace qualification_artifacts qualification_private qualification_rc qualification_bazel_binary qualification_root_bazel
cp -- MODULE.bazel.lock "$qualification_artifacts/MODULE.bazel.lock.before"
git cat-file -p HEAD > "$qualification_artifacts/commit.txt"
git status --porcelain --untracked-files=no > "$qualification_artifacts/source-before.txt"
[[ ! -s "$qualification_artifacts/source-before.txt" ]]
qualification_source_images=false
if [[ -n ${QUALIFICATION_PLAN_JSON:-} ]]; then
  printf '%s\n' "$QUALIFICATION_PLAN_JSON" > "$qualification_artifacts/matrix-plan.json"
  qualification_source_images=$(python3 test/rbe/unit_matrix.py dispatch-row \
    --plan "$qualification_artifacts/matrix-plan.json" --id "${QUALIFICATION_ROW_ID:?}" \
    --output "$qualification_artifacts/matrix-row.json")
fi
export qualification_source_images
QUALIFICATION_LANE_RESULTS=$qualification_artifacts/lane-results.jsonl
export QUALIFICATION_LANE_RESULTS
: > "$QUALIFICATION_LANE_RESULTS"

# Benchmark observations have a different coverage contract from named tests.
# Validate and retain the opt-in before any Bazel command can start.
qualification_benchmark_manifest=
qualification_benchmark_arguments=
benchmark_manifest_options=()
qualification_retain_outputs=false
if [[ ${lanes[*]} == pgo ]]; then
  [[ -n ${QUALIFICATION_TARGETS:-} ]]
  QUALIFICATION_BENCHMARK_MANIFEST=$qualification_workspace/test/benchmarks/pgo.json
  benchmark_manifest_options=(--selected-manifest "$qualification_artifacts/benchmark-manifest.json")
  qualification_retain_outputs=true
fi
if [[ -n ${QUALIFICATION_BENCHMARK_MANIFEST:-} ]]; then
  [[ ( ${lanes[*]} == test || ${lanes[*]} == pgo ) && -n ${QUALIFICATION_TARGETS:-} ]]
  qualification_benchmark_manifest=$qualification_artifacts/benchmark-manifest.json
  qualification_benchmark_arguments=$qualification_artifacts/benchmark-arguments.txt
  cp -- "$QUALIFICATION_BENCHMARK_MANIFEST" "$qualification_benchmark_manifest"
  benchmark_target_options=()
  for target in "${selected_targets[@]}"; do
    benchmark_target_options+=("--target=$target")
  done
  python3 test/rbe/qualification_report.py benchmark-arguments \
    --manifest "$qualification_benchmark_manifest" --output "$qualification_benchmark_arguments" \
    "${benchmark_target_options[@]}" "${benchmark_manifest_options[@]}"
  sha256sum "$qualification_benchmark_manifest" > "$qualification_artifacts/benchmark-manifest.sha256"
fi
# Mixed suites retain their source-declared benchmark obligations without
# changing arguments for ordinary tests in the same invocation.
qualification_declared_benchmarks=$qualification_artifacts/declared-benchmarks.json
cp test/benchmarks/lifecycle.json "$qualification_declared_benchmarks"
sha256sum "$qualification_declared_benchmarks" > "$qualification_artifacts/declared-benchmarks.sha256"
export qualification_benchmark_manifest qualification_benchmark_arguments qualification_retain_outputs qualification_declared_benchmarks
if [[ -v QUALIFICATION_REQUIRED_CASES && -v QUALIFICATION_GO_REQUIRED_CASES ]]; then
  printf 'Select either XML required cases or Go required cases, not both.\n' >&2
  exit 2
fi
if [[ -v QUALIFICATION_GO_REQUIRED_CASES ]]; then
  [[ ${lanes[*]} == test && ${#selected_targets[@]} -gt 0 && -z $qualification_benchmark_manifest ]]
  cp -- "$QUALIFICATION_GO_REQUIRED_CASES" "$qualification_artifacts/go-cases.json"
  sha256sum "$qualification_artifacts/go-cases.json" > "$qualification_artifacts/go-cases.sha256"
fi
if [[ -v QUALIFICATION_REQUIRED_CASES ]]; then
  [[ ${lanes[*]} == test && ${#selected_targets[@]} -gt 0 && -z $qualification_benchmark_manifest ]]
  cp -- "$QUALIFICATION_REQUIRED_CASES" "$qualification_artifacts/required-cases.json"
  sha256sum "$qualification_artifacts/required-cases.json" > "$qualification_artifacts/required-cases.sha256"
fi

case "${QUALIFICATION_EXECUTION:-remote}" in
  remote) qualification_bazel_binary=$(type -P bazel) ;;
  local|remote-actions)
    # Actions adds credentials and runner settings; keep those temporary.
    qualification_rc=$(mktemp)
    temporary_files+=("$qualification_rc")
    cp -- "$qualification_workspace/test/rbe/qualification.bazelrc" "$qualification_rc"
    if [[ $QUALIFICATION_EXECUTION == local ]]; then
      options+=(--test-execution=local)
      case "${lanes[*]}" in
        unit|container|syscalls|syscalls-save|syscalls-resume|syscalls-kvm)
          options+=("--local-test-requirements=${QUALIFICATION_LOCAL_TEST_REQUIREMENTS-namespace}")
          ;;
      esac
    fi
    # Keep the compiler and toolchain actions on the existing RBE platforms.
    # The repository version, not the runner image's default, selects Bazel.
    export USE_BAZEL_VERSION
    USE_BAZEL_VERSION=$(<images/default/bazelversion)
    command -v bazelisk
    : "${BUILDBUDDY_API_KEY:?Configure the BUILDBUDDY_API_KEY repository secret}"
    qualification_bazel_binary=$(command -v bazelisk)
    # Bazel's Docker strategy runs each test as the coordinator's UID. These
    # lanes need root for their namespace fixture or KVM device; others retain
    # the nonroot server.
    # https://github.com/bazelbuild/bazel/blob/f8278f94e/src/main/java/com/google/devtools/build/lib/sandbox/DockerSandboxedSpawnRunner.java#L267-L274
    qualification_root_bazel=false
    if [[ $QUALIFICATION_EXECUTION == local ]]; then
      case "$QUALIFICATION_ARCH:${lanes[*]}" in
        amd64:syscalls-rc-pilot|amd64:syscalls-rc|all:syscalls-rc|amd64:container|amd64:container-v1|amd64:container-v1-pilot|amd64:plugin-network|amd64:nftables|amd64:moby|amd64:kvm|amd64:language-goferfs|amd64:syscalls|amd64:syscalls-save|amd64:startup|amd64:posture|amd64:portforward|amd64:root|amd64:benchmarks|amd64:pgo|arm64:docker|arm64:cpu-images|arm64:gpu-images)
          qualification_root_bazel=true
          ;;
      esac
    fi
    export qualification_root_bazel
    qualification_container=false
    case "${lanes[*]}" in
      container|container-v1|container-v1-pilot) qualification_container=true ;;
    esac
    export qualification_container
    qualification_full_unit=false
    if [[ $QUALIFICATION_EXECUTION == local && $QUALIFICATION_ARCH == all && ${lanes[*]} == unit ]]; then
      qualification_full_unit=true
    fi
    export qualification_full_unit
    qualification_retain_reports=false
    if [[ $QUALIFICATION_EXECUTION == remote-actions ||
          $qualification_full_unit == true ||
          ( $QUALIFICATION_EXECUTION == local &&
            ( $qualification_container == true || ${lanes[*]} == test ||
              ${lanes[*]} == syscalls || ${lanes[*]} == syscalls-kvm ||
              ${lanes[*]} == syscalls-save || ${lanes[*]} == syscalls-resume ||
              ${lanes[*]} == pgo || ${lanes[*]} == docker || ${lanes[*]} == cpu-images || ${lanes[*]} == gpu-images ) ) ]]; then
      qualification_retain_reports=true
    fi
    export qualification_retain_reports
    if [[ $QUALIFICATION_EXECUTION == local &&
          ( ${lanes[*]} == language-goferfs || ${lanes[*]} == moby || ${lanes[*]} == kvm || ${lanes[*]} == benchmarks || ${lanes[*]} == pgo || ${lanes[*]} == docker || ${lanes[*]} == cpu-images || ${lanes[*]} == gpu-images ) ]]; then
      # Docker owns routing, NAT and endpoint teardown. A user-defined bridge
      # keeps each nested daemon's firewall in its own network namespace.
      [[ -S /var/run/docker.sock ]]
      GVISOR_DOCKER_NETWORK=$(sudo -n docker --host=unix:///var/run/docker.sock network create \
        --driver bridge "gvisor-qualification-${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}")
      [[ $GVISOR_DOCKER_NETWORK =~ ^[0-9a-f]{64}$ ]]
      export GVISOR_DOCKER_NETWORK
    fi
    {
      printf '%s\n' \
        'common --lockfile_mode=error' \
        'build:buildbuddy_remote_executor --remote_executor=grpcs://remote.buildbuddy.io' \
        'build:buildbuddy_remote_executor --remote_cache=grpcs://remote.buildbuddy.io' \
        'build:buildbuddy_remote_executor --bes_backend=grpcs://remote.buildbuddy.io' \
        'build:buildbuddy_remote_executor --bes_results_url=https://app.buildbuddy.io/invocation/'
      # Root Bazel does not inherit the GitHub environment. Record the checked
      # checkout identity explicitly for both coordinator users.
      printf 'build:buildbuddy_remote_executor --build_metadata=COMMIT_SHA=%s\n' "$QUALIFICATION_COMMIT"
      printf 'build:buildbuddy_remote_executor --build_metadata=REPO_URL=%s/%s\n' "$GITHUB_SERVER_URL" "$GITHUB_REPOSITORY"
      printf 'build:buildbuddy_remote_executor --build_metadata=BRANCH_NAME=%s\n' "$GITHUB_REF_NAME"
      printf 'build:buildbuddy_remote_executor --remote_header=x-buildbuddy-api-key=%s\n' "$BUILDBUDDY_API_KEY"
      printf 'build:buildbuddy_remote_executor --bes_header=x-buildbuddy-api-key=%s\n' "$BUILDBUDDY_API_KEY"
      if [[ $qualification_full_unit == true || $QUALIFICATION_EXECUTION == remote-actions ]]; then
        # Broad graphs need space beyond their roughly 4-GiB retained heap.
        # On this 16-GiB runner, give Bazel 8 GiB and local actions 4 GiB,
        # leaving the remainder for native overhead and the operating system.
        printf '%s\n' \
          'startup --host_jvm_args=-Xmx8g' \
          'build:buildbuddy_remote_executor --local_resources=memory=4096'
      fi
      if [[ $QUALIFICATION_EXECUTION == remote-actions ]]; then
        # Reuse shared input trees when preparing remote actions. Keep Bazel's
        # bounded cache size and CPU-sized preparation limit.
        printf '%s\n' 'build:buildbuddy_remote_executor --experimental_remote_merkle_tree_cache'
      fi
      if [[ ${lanes[*]} == pgo ]]; then
        printf '%s\n' 'build:buildbuddy_remote_executor --remote_download_regex=.*[/](test[.](log|xml)|test[.]outputs/.*)$'
      fi
      # Report retention must not force successful unit tests to run again.
      if [[ $qualification_retain_reports == true && $qualification_full_unit == false ]]; then
        printf '%s\n' \
          'test:buildbuddy_remote_executor --nocache_test_results' \
          'test:buildbuddy_remote_executor --runs_per_test=1' \
          'test:buildbuddy_remote_executor --flaky_test_attempts=1'
        # Minimal test workers need not contain zip. Bazel retains the files
        # directly, and the qualification collector accepts either format.
        printf '%s\n' 'test:buildbuddy_remote_executor --nozip_undeclared_test_outputs'
      fi
    } >> "$qualification_rc"
    policy_execution=remote
    if [[ $QUALIFICATION_EXECUTION == local ]]; then policy_execution=local; fi
    qualification_seconds=$(python3 test/rbe/unit_matrix.py coordinator-policy \
      "--architecture=$QUALIFICATION_ARCH" "--execution=$policy_execution" \
      "--requirements=${QUALIFICATION_LOCAL_TEST_REQUIREMENTS-namespace}" "${lanes[@]}")
    if (( qualification_seconds > 0 )); then
      QUALIFICATION_WORK_DEADLINE=$(python3 -c 'import sys,time; print(time.monotonic()+int(sys.argv[1]))' "$qualification_seconds")
      export QUALIFICATION_WORK_DEADLINE
    fi
    unset BUILDBUDDY_API_KEY

    # These are observations of this VM, not claims about other hosted images.
    {
      uname -a
      id
      printf 'page_size=%s\n' "$(getconf PAGESIZE)"
      printf 'logical_cpus=%s\n' "$(getconf _NPROCESSORS_ONLN)"
      awk '$1 == "MemTotal:" { print }' /proc/meminfo
      df -h "$PWD"
      ps -p 1 -o comm=
      stat -fc 'cgroup_filesystem=%T' /sys/fs/cgroup
      sysctl fs.suid_dumpable
      if [[ -d /sys/module/ip6_tables ]]; then
        printf 'ip6_tables sysfs entry before setup: present\n'
      else
        printf 'ip6_tables sysfs entry before setup: absent\n'
      fi
      for device in /dev/kvm /dev/vhost-net /dev/net/tun; do
        if [[ -c $device ]]; then
          ls -l "$device"
        else
          printf '%s: unavailable\n' "$device"
        fi
      done
      sysctl kernel.unprivileged_userns_clone kernel.apparmor_restrict_unprivileged_userns || true
      systemctl --version
      docker version || true
    } | tee "$RUNNER_TEMP/qualification/host.txt"
    if [[ $QUALIFICATION_EXECUTION == local ]]; then
      # The unchanged release smoke deliberately exercises unprivileged setup.
      [[ $(id -u) != 0 ]]
      [[ $(getconf PAGESIZE) == 4096 ]]
      sudo -n true
      # Match the Buildkite test-host setup on this ephemeral Actions VM.
      # Ubuntu's restriction prevents the rootless runtime's user namespace.
      if [[ $(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null) == 1 ]]; then
        sudo -n sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
        [[ $(sysctl -n kernel.apparmor_restrict_unprivileged_userns) == 0 ]]
      fi
      if [[ $qualification_root_bazel == true ]]; then
        # Private Docker daemons use legacy iptables inside their namespaces.
        # Load the table modules on the host before entering those namespaces.
        sudo -n modprobe iptable_filter
        sudo -n modprobe iptable_nat
      fi
      if [[ ${lanes[*]} == syscalls || ${lanes[*]} == syscalls-save || ${lanes[*]} == syscalls-resume || ${lanes[*]} == syscalls-kvm ]]; then
        # The maintained rtnetlink syscall owners invoke ip and OpenBSD nc.
        sudo -n apt-get update
        sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y iproute2 netcat-openbsd
        dpkg-query -W iproute2 netcat-openbsd | tee "$RUNNER_TEMP/qualification/network-tools.txt"
        command -v ip nc
      fi
      if [[ ${lanes[*]} == syscalls && $QUALIFICATION_ARCH == amd64 ]]; then
        # The AMD64 profile includes native IPv6 netfilter tests; their raw
        # sockopts do not load the legacy handler.
        sudo -n modprobe ip6_tables
      fi
      if [[ ${lanes[*]} == nftables || ${lanes[*]} == moby ]]; then
        # These suites exercise nftables in their private Docker namespaces.
        sudo -n modprobe nfnetlink
        sudo -n modprobe nf_tables
      fi
      if [[ ${lanes[*]} == plugin-network ]]; then
        # The plugin opens host vhost-net and TUN devices inside its sandbox.
        for device in /dev/vhost-net /dev/net/tun; do
          [[ -c $device ]]
          sudo -n test -r "$device"
          sudo -n test -w "$device"
        done
      fi
      if [[ $qualification_container == true || ${lanes[*]} == kvm || ${lanes[*]} == syscalls-kvm || ${lanes[*]} == syscalls-rc-pilot || ${lanes[*]} == syscalls-rc || ${lanes[*]} == pgo ]]; then
        [[ -c /dev/kvm ]]
        sudo -n test -r /dev/kvm
        sudo -n test -w /dev/kvm
      fi
    fi
    ;;
  *) printf 'Unknown qualification execution mode.\n' >&2; exit 2 ;;
esac

# Keep one execution/deadline policy for preflight, queries and actual work.
qualification_bazel() {
  local remaining
  local -a prefix=()
  if [[ $qualification_root_bazel == true ]]; then
    prefix=(sudo -n -H env "USE_BAZEL_VERSION=$USE_BAZEL_VERSION")
  fi
  if [[ -n ${QUALIFICATION_WORK_DEADLINE:-} ]]; then
    remaining=$(python3 -c 'import os,time; print(max(0,int(float(os.environ["QUALIFICATION_WORK_DEADLINE"])-time.monotonic())))')
    if (( remaining <= 0 )); then
      printf 'Qualification work deadline exhausted.\n' >&2
      return 124
    fi
    prefix+=(timeout --signal=INT --kill-after=30s "${remaining}s")
  fi
  "${prefix[@]}" "$qualification_bazel_binary" --bazelrc="$qualification_rc" "$@"
}
export -f qualification_bazel

bazel() {
  local argument command_kind="" result=0 capture_status=0 verdict_status=0 preflight_status=0 analysis_only=false
  local directory raw_events expected_events="" index command_only=false arguments_file=arguments.txt
  local -a capture_prefix=() capture_options=() verdict_options=() command_args=("$@") target_args=() selection_options=()
  local case_checksum=""
  local -a case_options=() go_case_options=() alias_options=()
  if [[ $qualification_retain_outputs == true ]]; then capture_options=(--retain-outputs); fi
  for argument in "$@"; do
    case "$argument" in
      --nobuild|--nobuild=true|--build=false) analysis_only=true ;;
      --*) ;;
      *) if [[ -z $command_kind ]]; then command_kind=$argument; fi ;;
    esac
  done
  if [[ $command_kind == test && -v QUALIFICATION_REQUIRED_CASES ]]; then
    case_options=(--required-cases "$qualification_artifacts/required-cases.json")
    case_checksum=$qualification_artifacts/required-cases.sha256
  fi
  if [[ $command_kind == run || $command_kind == query ]]; then
    command_only=true
    arguments_file=arguments.json
  elif [[ $command_kind != build && $command_kind != test ]]; then
    qualification_bazel "$@"
    return
  fi
  # Make separates target patterns with --; reporting options must precede it.
  for (( index=0; index<${#command_args[@]}; index++ )); do
    if [[ ${command_args[index]} == -- ]]; then
      target_args=("${command_args[@]:index}")
      command_args=("${command_args[@]:0:index}")
      break
    fi
  done
  if [[ $qualification_source_images == true && $command_only == false && $analysis_only == false ]]; then
    # Independent analysis must declare the same contract outputs as execution;
    # otherwise a missing actual contract could look inapplicable.
    command_args+=(--aspects=//test/docker:source_image_contracts.bzl%source_image_contracts)
  fi
  if [[ $command_kind == test && -n $qualification_benchmark_manifest ]]; then
    command_args+=(--nocache_test_results)
    while IFS= read -r argument; do
      command_args+=("--test_arg=$argument")
    done < "$qualification_benchmark_arguments"
  fi
  if [[ $qualification_root_bazel == true ]]; then capture_prefix=(sudo -n -H); fi
  # Some callers inspect Bazel's exit status with ||, which disables errexit
  # throughout this function. A failed evidence precondition must still stop
  # before actions start.
  if ! {
    directory=$(mktemp -d "$qualification_artifacts/invocation-XXXXXX") &&
      raw_events=$(mktemp "$qualification_private/events-XXXXXX.json") &&
      printf '%s\n' "${command_args[@]}" "${target_args[@]}" > "$directory/arguments.txt" &&
      git cat-file -p HEAD > "$directory/commit.txt" &&
      git status --porcelain --untracked-files=no > "$directory/source-before.txt" &&
      [[ ! -s "$directory/source-before.txt" ]]
  }; then
    return 1
  fi
  if [[ $command_only == true ]]; then
    # Keep argument boundaries, including literal newlines, in command evidence.
    python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@" > "$directory/$arguments_file" || return 1
  fi

  if [[ $command_kind == test && $analysis_only == false && -v QUALIFICATION_GO_REQUIRED_CASES ]]; then
    # Replace only command/target selection. Configuration and test options come
    # from this invocation, including the mixed-platform configuration chosen by
    # qualify.sh; callers must not reconstruct them for the authoring query.
    for argument in "${command_args[@]}"; do
      case "$argument" in
        test|--target_pattern_file=*|//*|@*//*) ;;
        --*) go_case_options+=("$argument") ;;
        *) printf 'Unsupported test argument for Go case analysis: %s\n' "$argument" >&2; return 2 ;;
      esac
    done
    printf '%s\n' "${go_case_options[@]}" > "$directory/go-case-options.txt" || return 1
    # Use the same startup RC, coordinator identity and remaining work deadline.
    printf '#!/bin/bash\nqualification_bazel "$@"\n' > "$qualification_private/go-case-bazel" || return 1
    chmod +x "$qualification_private/go-case-bazel" || return 1
    python3 "$qualification_workspace/test/rbe/go_required_cases.py" \
      "$qualification_artifacts/go-cases.json" "$directory/required-cases.json" \
      --evidence "$directory/go-case-analysis.json" \
      --bazel "$qualification_private/go-case-bazel" -- "${go_case_options[@]}" \
      > "$directory/go-case-stdout.log" 2> "$directory/go-case-stderr.log" || result=$?
    printf '%s\n' "$result" > "$directory/go-case-exit.txt" || return 1
    cat -- "$directory/go-case-stdout.log" || return 1
    cat -- "$directory/go-case-stderr.log" >&2 || return 1
    # An authoring failure is not a native test result. Bind the aborted
    # preparation to its source and requested labels without inventing owners.
    if (( result != 0 )); then
      git rev-parse HEAD > "$directory/final-head.txt" || return 1
      git status --porcelain --untracked-files=no > "$directory/source-after.txt" || return 1
      python3 - "$directory" <<'PREPARATION' || return 1
import json
import os
from pathlib import Path
import sys

directory = Path(sys.argv[1])
arguments = (directory / "arguments.txt").read_text().splitlines()
pattern_files = [argument.split("=", 1)[1] for argument in arguments
                 if argument.startswith("--target_pattern_file=")]
if len(pattern_files) > 1:
    raise ValueError("Expected at most one target pattern file")
if pattern_files:
    (directory / "target-patterns.txt").write_bytes(Path(pattern_files[0]).read_bytes())
(directory / "preparation-failure.json").write_text(json.dumps({
    "source": os.environ["QUALIFICATION_COMMIT"],
    "target_pattern_files": pattern_files,
}) + "\n")
PREPARATION
      return "$result"
    fi
    sha256sum --check "$qualification_artifacts/go-cases.sha256" >&2 || return 1
    case_checksum=$directory/required-cases.sha256
    sha256sum "$directory/required-cases.json" > "$case_checksum" || return 1
    case_options=(--required-cases "$directory/required-cases.json")
  fi

  # Resolve the exact command before starting its actions. Besides catching
  # invalid configuration early, this supplies an independent expected target set
  # so a truncated result stream cannot silently lose selected owners.
  if [[ $command_only == false && $analysis_only == false ]]; then
    expected_events=$(mktemp "$qualification_private/expected-XXXXXX.json")
    # Test filters otherwise leave build-only roots in the analysis result.
    # Keep those builds in the actual command, but not the expected test set.
    if [[ $command_kind == test ]]; then selection_options=(--build_tests_only); fi
    qualification_bazel "${command_args[@]}" --nobuild "${selection_options[@]}" \
      "--build_event_json_file=$expected_events" --build_event_json_file_path_conversion=false \
      "${target_args[@]}" || result=$?
    printf '%s\n' "$result" > "$directory/preflight-exit.txt" || capture_status=1
    "${capture_prefix[@]}" timeout --signal=TERM --kill-after=5s 300s \
      python3 "$qualification_workspace/test/rbe/qualification_report.py" capture \
      --events "$expected_events" --output "$directory/expected.jsonl" \
      --output-root "$qualification_workspace/bazel-out" --report-dir "$directory/reports" \
    "${capture_options[@]}" || capture_status=$?
    verdict_options+=(--expected-analysis "$directory/expected.jsonl")
    capture_options+=(--expected-analysis "$directory/expected.jsonl" --expected-kind "$command_kind")
    if (( capture_status == 0 )); then
      if python3 "$qualification_workspace/test/rbe/qualification_report.py" preflight \
        "${case_options[@]}" \
        --events "$directory/expected.jsonl" --native-exit "$result" --kind "$command_kind" \
        --output "$directory/selection.json" --output-options "$directory/output-options.txt" \
        --alias-query "$directory/source-image-alias-query.txt"; then
        result=0
      else
        preflight_status=$?
        if (( result == 0 )); then capture_status=$preflight_status; fi
      fi
    fi
  fi
  if (( result == 0 && capture_status == 0 )); then
    if [[ $command_only == true ]]; then
      # Queries may redirect stdout into another command's input. Replay each
      # original stream exactly once, without reporter status text on stdout.
      qualification_bazel "$@" > "$directory/stdout.log" 2> "$directory/stderr.log" || result=$?
    elif [[ $analysis_only == false ]]; then
      while IFS= read -r argument; do command_args+=("$argument"); done < "$directory/output-options.txt" || return 1
      if [[ $qualification_source_images == true ]]; then
        if [[ $qualification_retain_outputs == true ]]; then
          command_args+=('--remote_download_regex=.*([/]test[.](log|xml)|[/]test[.]outputs/.*|[.]source-images[.]json)$')
        else
          command_args+=('--remote_download_regex=.*([/]test[.](log|xml)|[.]source-images[.]json)$')
        fi
      fi
      printf '%s\n' "${command_args[@]}" "${target_args[@]}" > "$directory/arguments.txt" || return 1
    fi
    if [[ $command_only == false ]]; then
      qualification_bazel "${command_args[@]}" \
        "--execution_log_compact_file=$directory/execution.binpb" \
        "--profile=$directory/profile.json.gz" \
        "--build_event_json_file=$raw_events" --build_event_json_file_path_conversion=false \
        "${target_args[@]}" || result=$?
    fi
  fi
  python3 -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat())' \
    > "$directory/execution-finished.txt" || capture_status=1
  printf '%s\n' "$result" > "$directory/native-exit.txt" || capture_status=1
  # Resolve actual labels and configurations for aliases without rerunning any
  # actions or letting a diagnostic query suppress the native command.
  if [[ $qualification_source_images == true && -s "$directory/source-image-alias-query.txt" ]]; then
    for argument in "${command_args[@]}"; do
      case "$argument" in
        --target_pattern_file=*) ;;
        --*) alias_options+=("$argument") ;;
      esac
    done
    qualification_bazel cquery "${alias_options[@]}" --output=jsonproto --transitions=lite \
      --proto:rule_attributes=actual --proto:rule_inputs_and_outputs \
      "$(<"$directory/source-image-alias-query.txt")" > "$directory/source-image-aliases.json" || capture_status=$?
  fi
  git rev-parse HEAD > "$directory/final-head.txt" || capture_status=1
  git status --porcelain --untracked-files=no > "$directory/source-after.txt" || capture_status=1
  if [[ $command_only == true ]]; then
    cat -- "$directory/stdout.log" || capture_status=$?
    cat -- "$directory/stderr.log" >&2 || capture_status=$?
  else
    "${capture_prefix[@]}" timeout --signal=TERM --kill-after=5s 300s \
      python3 "$qualification_workspace/test/rbe/qualification_report.py" capture \
      --events "$raw_events" --output "$directory/events.jsonl" \
      --output-root "$qualification_workspace/bazel-out" --report-dir "$directory/reports" \
      "${capture_options[@]}" || capture_status=$?
  fi
  if [[ -n $qualification_benchmark_manifest ]]; then
    sha256sum --check "$qualification_artifacts/benchmark-manifest.sha256" >&2 || capture_status=1
  fi
  if (( ${#case_options[@]} )); then
    sha256sum --check "$case_checksum" >&2 || capture_status=1
  fi
  sha256sum --check "$qualification_artifacts/declared-benchmarks.sha256" >&2 || capture_status=1
  printf '%s\n' "$capture_status" > "$directory/capture-exit.txt" || capture_status=1
  if [[ $command_only == true ]]; then
    command_kind=command
  elif [[ $analysis_only == true ]]; then
    command_kind=analysis
  elif [[ $command_kind == test && -n $qualification_benchmark_manifest ]]; then
    command_kind=benchmark
    verdict_options+=(--expected-benchmarks "$qualification_benchmark_manifest")
  elif [[ $command_kind == test ]]; then
    verdict_options+=(--declared-benchmarks "$qualification_declared_benchmarks" "${case_options[@]}")
  fi
  python3 - "$directory" "$QUALIFICATION_COMMIT" "$command_kind" "$arguments_file" <<'CONTEXT' || capture_status=$?
import json
from pathlib import Path
import sys

directory = Path(sys.argv[1])
try:
    finished = (directory / "execution-finished.txt").read_text().strip()
except OSError:
    finished = None
context = {
    "expected_source": sys.argv[2],
    "command_kind": sys.argv[3],
    "execution_finished_at": finished,
    "commit_file": "commit.txt",
    "source_before": "source-before.txt",
    "source_after": "source-after.txt",
    "final_head": "final-head.txt",
    "executed_arguments": sys.argv[4],
    "native_exit": "native-exit.txt",
    "capture_exit": "capture-exit.txt",
}
(directory / "context.json").write_text(json.dumps(context) + "\n")
CONTEXT
  if [[ $command_only == true ]]; then
    verdict_options=(command-verdict)
  else
    verdict_options=(verdict --events "$directory/events.jsonl" --manifest "$directory/events.jsonl.manifest.json" "${verdict_options[@]}")
  fi
  "${capture_prefix[@]}" timeout --signal=TERM --kill-after=5s 300s \
    python3 "$qualification_workspace/test/rbe/qualification_report.py" "${verdict_options[@]}" \
    --context "$directory/context.json" --output "$directory/verdict.json" \
    || verdict_status=$?
  if [[ $command_only == false && $analysis_only == true ]] && (( capture_status == 0 && verdict_status == 0 )); then
    return 0
  fi
  if (( result == 0 )); then
    if (( capture_status != 0 )); then result=$capture_status; else result=$verdict_status; fi
  fi
  return "$result"
}
export -f bazel

if [[ -n $QUALIFICATION_HEADER_BASE && ( -z ${QUALIFICATION_PLAN_JSON:-} || " ${lanes[*]} " == *" license-headers "* ) ]]; then
  if [[ ! $QUALIFICATION_HEADER_BASE =~ ^[0-9a-f]{40}$ ]]; then
    printf 'Supply the full header comparison commit SHA.\n' >&2
    exit 2
  fi
  # The remote checkout is separate from Actions and may be shallow. Fetch both
  # histories so qualify.sh can determine the exact added-file selection.
  fetch_options=()
  if [[ $(git rev-parse --is-shallow-repository) == true ]]; then
    fetch_options+=(--unshallow)
  fi
  git fetch "${fetch_options[@]}" origin "$QUALIFICATION_COMMIT" "$QUALIFICATION_HEADER_BASE"
  options+=("--header-base=$QUALIFICATION_HEADER_BASE")
fi

if [[ ( -z ${QUALIFICATION_PLAN_JSON:-} || " ${lanes[*]} " == *" cos-metadata "* ) && ( -n $QUALIFICATION_COS_GZIP_BASE64 || -n $QUALIFICATION_COS_SHA256 ) ]]; then
  if [[ -z $QUALIFICATION_COS_GZIP_BASE64 || ! $QUALIFICATION_COS_SHA256 =~ ^[0-9a-f]{64}$ ]]; then
    printf 'Supply both a gzip/base64 COS catalog and its uncompressed SHA256.\n' >&2
    exit 2
  fi
  COS_IMAGES_JSON=$(mktemp)
  export COS_IMAGES_JSON
  temporary_files+=("$COS_IMAGES_JSON")
  printf '%s' "$QUALIFICATION_COS_GZIP_BASE64" | base64 --decode | gzip --decompress > "$COS_IMAGES_JSON"
  printf '%s  %s\n' "$QUALIFICATION_COS_SHA256" "$COS_IMAGES_JSON" | sha256sum --check --strict
fi

test/rbe/qualify.sh "${options[@]}" "$@" "${lanes[@]}"
