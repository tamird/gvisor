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

# Root test fixture for disposable Actions VMs with an unprivileged Bazel server.
set -euo pipefail
if (( EUID != 0 )); then
  exec sudo -n -E -- unshare --mount --propagation private -- "$0" "$@"
fi
[[ ${SUDO_UID:?} =~ ^[0-9]+$ && $SUDO_UID != 0 && ${SUDO_GID:?} =~ ^[0-9]+$ ]]
out=${TEST_UNDECLARED_OUTPUTS_DIR:?}
[[ -d $out && ! -L $out ]]
test_tmp=${TEST_TMPDIR:?}
test_directories=("$test_tmp")
if [[ -n ${TEST_PREMATURE_EXIT_FILE:-} ]]; then
  test_directories+=("$(dirname "$TEST_PREMATURE_EXIT_FILE")")
fi
cleanup() {
  local status=$?
  trap - EXIT
  local outputs=("$out" "$test_tmp")
  # Go tests write XML as root; Bazel must own it to normalize output permissions.
  # Other tests leave XML generation to Bazel after this fixture exits.
  if [[ -e ${XML_OUTPUT_FILE:?} || -L $XML_OUTPUT_FILE ]]; then
    outputs+=("$XML_OUTPUT_FILE")
  fi
  if [[ -n ${TEST_PREMATURE_EXIT_FILE:-} && ( -e $TEST_PREMATURE_EXIT_FILE || -L $TEST_PREMATURE_EXIT_FILE ) ]]; then
    outputs+=("$TEST_PREMATURE_EXIT_FILE")
  fi
  # Do not follow output symlinks or change ownership elsewhere in the cache.
  local cleanup_status=0
  chown -hR "$SUDO_UID:$SUDO_GID" -- "${outputs[@]}" || cleanup_status=1
  chown -h "$SUDO_UID:$SUDO_GID" -- "${test_directories[@]}" || cleanup_status=1
  if (( cleanup_status != 0 )); then
    printf 'Failed to return test output ownership to Bazel.\n' >&2
    if (( status == 0 )); then status=1; fi
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# The syscall runner maps guest root to host root. Bazel's unprivileged
# directories must belong to that identity for scratch files and GTest's
# premature-exit marker; keep their existing modes and marker protocol.
for directory in "${test_directories[@]}"; do
  [[ -d $directory && ! -L $directory ]]
  stat -c 'Test directory before: %a %u:%g %n' "$directory"
  chown root:root -- "$directory"
  stat -c 'Test directory ready: %a %u:%g %n' "$directory"
done

# Overlay backing files must not hold the gofer's root mount writable. Give the
# existing scratch directory its own mount without changing its filesystem.
findmnt --target "$test_tmp" --output ID,TARGET,FSTYPE,OPTIONS
mount --bind "$test_tmp" "$test_tmp"
findmnt --target "$test_tmp" --output ID,TARGET,FSTYPE,OPTIONS

# Some root tests re-exec the declared runtime as nobody. Grant directory
# traversal only; leave file modes and data, including the credential RC, alone.
runtime=${TEST_SRCDIR:?}/${TEST_WORKSPACE:?}/release/runsc
if [[ -e $runtime ]]; then
  runtime=$(readlink -f "$runtime")
  namei -l "$runtime"
  [[ $(stat -c %a "$runtime") =~ [1357]$ ]]
  directory=$(dirname "$runtime")
  while [[ $directory != / ]]; do
    if [[ ! $(stat -c %a "$directory") =~ [1357]$ ]]; then
      chmod o+x "$directory"
      stat -c 'Runtime ancestor: %a %n' "$directory"
    fi
    directory=$(dirname "$directory")
  done
fi
"$@"
