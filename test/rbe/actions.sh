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
options=("--arch=$QUALIFICATION_ARCH")
if [[ -n ${QUALIFICATION_SYSCALL_BUCKET:-} ]]; then
  options+=("--syscall-bucket=$QUALIFICATION_SYSCALL_BUCKET")
fi
if [[ -n ${QUALIFICATION_BENCHMARK_TARGET:-} ]]; then
  options+=("--benchmark-target=$QUALIFICATION_BENCHMARK_TARGET")
fi
temporary_files=()
GVISOR_DOCKER_NETWORK=
cleanup() {
  local status=$?
  rm -f -- "${temporary_files[@]}"
  if [[ -n $GVISOR_DOCKER_NETWORK ]]; then
    # Bazel has removed its --rm test containers before returning. An endpoint
    # left behind is a cleanup failure, not a reason to force-disconnect it.
    if ! sudo -n docker --host=unix:///var/run/docker.sock network rm "$GVISOR_DOCKER_NETWORK"; then
      if (( status == 0 )); then status=1; fi
    fi
  fi
  if [[ ${lanes[*]} == language-goferfs && -d ${RUNNER_TEMP:-}/qualification ]]; then
    git rev-parse HEAD > "$RUNNER_TEMP/qualification/source-after-head.txt" || status=1
    git status --porcelain --untracked-files=no > "$RUNNER_TEMP/qualification/source-after.txt" || status=1
    [[ $(<"$RUNNER_TEMP/qualification/source-after-head.txt") == "$QUALIFICATION_COMMIT" ]] || status=1
    [[ ! -s $RUNNER_TEMP/qualification/source-after.txt ]] || status=1
  fi
  exit "$status"
}
trap cleanup EXIT

case "${QUALIFICATION_EXECUTION:-remote}" in
  remote) ;;
  local)
    options+=(--test-execution=local)
    # Keep the compiler and toolchain actions on the existing RBE platforms.
    # The repository version, not the runner image's default, selects Bazel.
    export USE_BAZEL_VERSION
    USE_BAZEL_VERSION=$(<images/default/bazelversion)
    command -v bazelisk
    : "${BUILDBUDDY_API_KEY:?Configure the BUILDBUDDY_API_KEY repository secret}"
    qualification_rc=$(mktemp)
    temporary_files+=("$qualification_rc")
    export qualification_rc
    # Bazel's Docker strategy runs each test as the coordinator's UID. These
    # lanes need root for their namespace fixture; others retain the nonroot server.
    # https://github.com/bazelbuild/bazel/blob/f8278f94e/src/main/java/com/google/devtools/build/lib/sandbox/DockerSandboxedSpawnRunner.java#L267-L274
    qualification_root_bazel=false
    case "$QUALIFICATION_ARCH:${lanes[*]}" in
      amd64:plugin-network|amd64:nftables|amd64:syscalls|amd64:startup|amd64:posture|amd64:portforward|amd64:root|amd64:benchmarks|amd64:language-goferfs|arm64:docker|arm64:cpu-images|arm64:gpu-images)
        qualification_root_bazel=true
        ;;
    esac
    export qualification_root_bazel
    if [[ ${lanes[*]} == benchmarks || ${lanes[*]} == docker || ${lanes[*]} == cpu-images || ${lanes[*]} == gpu-images || ${lanes[*]} == language-goferfs ]]; then
      # Docker owns routing, NAT and endpoint teardown. A user-defined bridge
      # keeps each nested daemon's firewall in its own network namespace.
      [[ -S /var/run/docker.sock ]]
      GVISOR_DOCKER_NETWORK=$(sudo -n docker --host=unix:///var/run/docker.sock network create \
        --driver bridge "gvisor-qualification-${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}")
      [[ $GVISOR_DOCKER_NETWORK =~ ^[0-9a-f]{64}$ ]]
      export GVISOR_DOCKER_NETWORK
    fi
    if [[ ${lanes[*]} == language-goferfs ]]; then
      # Reuse the exact source-built PHP image from the failed qualification.
      # The complete hash is checked before Bazel can consume the declared file.
      php_image="$PWD/test/runtimes/php_actions_image.tar"
      [[ ! -e $php_image && ! -e $php_image.partial ]]
      temporary_files+=("$php_image" "$php_image.partial")
      mkdir -p "$RUNNER_TEMP/qualification"
      timeout --signal=TERM --kill-after=5s 300s python3 - \
        "$php_image" "$RUNNER_TEMP/qualification/php-image.json" <<'PHP_IMAGE'
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import urllib.request

path = Path(sys.argv[1])
partial = path.with_suffix(path.suffix + ".partial")
expected_hash = "b2c8e50d4da59ec192f0c83bd2c38b6031b6efe5db5cd24ac347e5e3e6207741"
expected_size = 1062409728
assert not path.exists() and not partial.exists()
assert shutil.disk_usage(path.parent).free > expected_size + 1024 * 1024 * 1024
uri = "bytestream://remote.buildbuddy.io/blobs/" + expected_hash + "/" + str(expected_size)
request = urllib.request.Request(
    "https://app.buildbuddy.io/api/v1/GetFile",
    data=json.dumps({"uri": uri}).encode(),
    headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]},
)
size = 0
hasher = hashlib.sha256()
with urllib.request.urlopen(request, timeout=60) as response, partial.open("xb") as destination:
    assert response.status == 200, response.status
    while chunk := response.read(1024 * 1024):
        size += len(chunk)
        assert size <= expected_size, size
        hasher.update(chunk)
        destination.write(chunk)
assert size == expected_size and hasher.hexdigest() == expected_hash, (size, hasher.hexdigest())
partial.replace(path)
Path(sys.argv[2]).write_text(json.dumps({
    "sourceParent": "9f65f5c6-a344-47e0-a4f0-76bbeb67cbcc",
    "uri": uri,
    "sha256": expected_hash,
    "size": size,
    "declaredInput": "test/runtimes/php_actions_image.tar",
    "reusedSourceBuiltImage": True,
}, indent=2) + "\n")
PHP_IMAGE
    fi
    {
      printf '%s\n' \
        'build:buildbuddy_remote_executor --remote_executor=grpcs://remote.buildbuddy.io' \
        'build:buildbuddy_remote_executor --remote_cache=grpcs://remote.buildbuddy.io' \
        'build:buildbuddy_remote_executor --bes_backend=grpcs://remote.buildbuddy.io' \
        'build:buildbuddy_remote_executor --bes_results_url=https://app.buildbuddy.io/invocation/'
      # Root Bazel does not inherit the GitHub environment. Preserve the
      # checked-out source identity in the existing Actions adapter.
      printf 'build:buildbuddy_remote_executor --build_metadata=COMMIT_SHA=%s\n' "$QUALIFICATION_COMMIT"
      printf 'build:buildbuddy_remote_executor --build_metadata=REPO_URL=%s/%s\n' "$GITHUB_SERVER_URL" "$GITHUB_REPOSITORY"
      printf 'build:buildbuddy_remote_executor --build_metadata=BRANCH_NAME=%s\n' "$GITHUB_REF_NAME"
      printf 'build:buildbuddy_remote_executor --remote_header=x-buildbuddy-api-key=%s\n' "$BUILDBUDDY_API_KEY"
      printf 'build:buildbuddy_remote_executor --bes_header=x-buildbuddy-api-key=%s\n' "$BUILDBUDDY_API_KEY"
    } > "$qualification_rc"
    unset BUILDBUDDY_API_KEY
    # Capture spawn placement without including the credential RC in artifacts.
    mkdir -p "$RUNNER_TEMP/qualification"
    bazel() {
      local argument
      local -a evidence=()
      # Startup options may precede the command. Queries perform no spawns and
      # do not accept execution-log options; each build/test keeps its own log.
      for argument in "$@"; do
        if [[ $argument == --* ]]; then
          continue
        fi
        if [[ $argument == build || $argument == test ]]; then
          evidence=(
            "--execution_log_compact_file=$(mktemp "$RUNNER_TEMP/qualification/execution-XXXXXX.binpb")"
          )
        fi
        break
      done
      if [[ $qualification_root_bazel == true ]]; then
        sudo -n -H env "USE_BAZEL_VERSION=$USE_BAZEL_VERSION" \
          "$(command -v bazelisk)" --bazelrc="$qualification_rc" "$@" "${evidence[@]}"
      else
        command bazelisk --bazelrc="$qualification_rc" "$@" "${evidence[@]}"
      fi
    }
    export -f bazel

    # These are observations of this VM, not claims about other hosted images.
    {
      uname -a
      id
      printf 'page_size=%s\n' "$(getconf PAGESIZE)"
      printf 'logical_cpus=%s\n' "$(getconf _NPROCESSORS_ONLN)"
      df -h "$PWD"
      ps -p 1 -o comm=
      stat -fc 'cgroup_filesystem=%T' /sys/fs/cgroup
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
    if [[ ${lanes[*]} == syscalls || ${lanes[*]} == syscalls-resume || ${lanes[*]} == syscalls-kvm ]]; then
      # The maintained rtnetlink syscall owners invoke ip and OpenBSD nc.
      sudo -n apt-get update
      sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y iproute2 netcat-openbsd
      dpkg-query -W iproute2 netcat-openbsd | tee "$RUNNER_TEMP/qualification/network-tools.txt"
      command -v ip nc
    fi
    if [[ ${lanes[*]} == nftables ]]; then
      # Match both public nftables Make targets' host kernel setup.
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
    if [[ ${lanes[*]} == syscalls-kvm ]]; then
      [[ -c /dev/kvm ]]
      sudo -n test -r /dev/kvm
      sudo -n test -w /dev/kvm
    fi
    ;;
  *) printf 'Unknown qualification execution mode.\n' >&2; exit 2 ;;
esac

if [[ -n $QUALIFICATION_HEADER_BASE ]]; then
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

if [[ -n $QUALIFICATION_COS_GZIP_BASE64 || -n $QUALIFICATION_COS_SHA256 ]]; then
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

test/rbe/qualify.sh "${options[@]}" "${lanes[@]}"
