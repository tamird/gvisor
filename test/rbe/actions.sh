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
temporary_files=()
trap 'rm -f -- "${temporary_files[@]}"' EXIT

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
    {
      printf '%s\n' \
        'build:buildbuddy_remote_executor --remote_executor=grpcs://remote.buildbuddy.io' \
        'build:buildbuddy_remote_executor --remote_cache=grpcs://remote.buildbuddy.io' \
        'build:buildbuddy_remote_executor --bes_backend=grpcs://remote.buildbuddy.io' \
        'build:buildbuddy_remote_executor --bes_results_url=https://app.buildbuddy.io/invocation/'
      printf 'build:buildbuddy_remote_executor --remote_header=x-buildbuddy-api-key=%s\n' "$BUILDBUDDY_API_KEY"
      printf 'build:buildbuddy_remote_executor --bes_header=x-buildbuddy-api-key=%s\n' "$BUILDBUDDY_API_KEY"
    } > "$qualification_rc"
    unset BUILDBUDDY_API_KEY
    # Capture spawn placement without including the credential RC in artifacts.
    mkdir -p "$RUNNER_TEMP/qualification"
    export qualification_execution_log="$RUNNER_TEMP/qualification/execution.binpb"
    bazel() {
      command bazelisk --bazelrc="$qualification_rc" "$@" \
        "--execution_log_compact_file=$qualification_execution_log"
    }
    export -f bazel

    # These are observations of this VM, not claims about other hosted images.
    {
      uname -a
      id
      printf 'page_size=%s\n' "$(getconf PAGESIZE)"
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
