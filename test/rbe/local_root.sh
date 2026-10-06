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
  exec sudo -n -E -- "$0" "$@"
fi
[[ ${SUDO_UID:?} =~ ^[0-9]+$ && $SUDO_UID != 0 && ${SUDO_GID:?} =~ ^[0-9]+$ ]]
out=${TEST_UNDECLARED_OUTPUTS_DIR:?}
[[ -d $out && ! -L $out ]]
cleanup() {
  local status=$?
  trap - EXIT
  # Do not follow output symlinks or change ownership elsewhere in the cache.
  if ! chown -hR "$SUDO_UID:$SUDO_GID" -- "$out"; then
    printf 'Failed to return test output ownership to Bazel.\n' >&2
    if (( status == 0 )); then status=1; fi
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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
