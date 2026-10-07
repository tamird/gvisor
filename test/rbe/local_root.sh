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

# Root test fixture for disposable Actions VMs.
set -euo pipefail
initial_cgroup=false
if [[ ${1:-} == --initial-cgroup-namespace ]]; then
  initial_cgroup=true
  shift
fi
if (( EUID != 0 )); then
  [[ $initial_cgroup == false ]]
  exec sudo -n -E -- unshare --mount --propagation private -- "$0" "$@"
fi
if [[ $initial_cgroup == true ]]; then
  # The frontend acquires root with sudo, retaining its Bazel coordinator's
  # output identity and the hosted VM's original PID and cgroup namespaces.
  [[ $(readlink /proc/self/ns/pid) == "${GVISOR_HOST_PID_NS:?}" ]]
  [[ $(readlink /proc/self/ns/cgroup) == "${GVISOR_HOST_CGROUP_NS:?}" ]]
  [[ $(readlink /proc/self/ns/mnt) != "${GVISOR_HOST_MOUNT_NS:?}" ]]
  [[ $(stat -f -c %T /sys/fs/cgroup) == cgroup2fs ]]
  [[ ! -e /sys/fs/cgroup/cgroup.type && ! -e /sys/fs/cgroup/test ]]
else
  [[ ${SUDO_UID:?} =~ ^[0-9]+$ && $SUDO_UID != 0 && ${SUDO_GID:?} =~ ^[0-9]+$ ]]
fi
[[ ${SUDO_UID:?} =~ ^[0-9]+$ && ${SUDO_GID:?} =~ ^[0-9]+$ ]]
output_uid=$SUDO_UID
output_gid=$SUDO_GID
out=${TEST_UNDECLARED_OUTPUTS_DIR:?}
[[ -d $out && ! -L $out ]]
test_tmp=${TEST_TMPDIR:?}
[[ -d $test_tmp && ! -L $test_tmp ]]
if [[ $initial_cgroup == true ]]; then
  [[ $(stat -c %u "$out") == "$output_uid" && $(stat -c %u "$test_tmp") == "$output_uid" ]]
fi
original_controllers=
original_cgroup_options=
original_membership=
restore_cgroup_state() {
  local failed=0 controller current_options current_membership
  local -a current_controllers=() saved_controllers=()
  read -r -a current_controllers < /sys/fs/cgroup/cgroup.subtree_control || failed=1
  read -r -a saved_controllers <<< "$original_controllers"
  for controller in "${current_controllers[@]}"; do
    if [[ " $original_controllers " != *" $controller "* ]]; then
      printf '%s\n' "-$controller" > /sys/fs/cgroup/cgroup.subtree_control || failed=1
    fi
  done
  for controller in "${saved_controllers[@]}"; do
    if [[ " ${current_controllers[*]} " != *" $controller "* ]]; then
      printf '+%s\n' "$controller" > /sys/fs/cgroup/cgroup.subtree_control || failed=1
    fi
  done
  # Cgroup2 mount flags are global, including flags other than nsdelegate.
  # Restore the complete observed option set through a private temporary mount.
  local restore_mount=$test_tmp/cgroup-state-restore
  if mkdir "$restore_mount"; then
    mount -t cgroup2 -o "$original_cgroup_options" none "$restore_mount" || failed=1
    if mountpoint -q "$restore_mount"; then
      umount "$restore_mount" || failed=1
    fi
    rmdir "$restore_mount" || failed=1
  else
    failed=1
  fi
  current_options=$(findmnt --noheadings --first-only --target /sys/fs/cgroup --output FS-OPTIONS) || failed=1
  current_membership=$(< /proc/self/cgroup) || failed=1
  {
    printf 'controllers=%s\n' "$(< /sys/fs/cgroup/cgroup.subtree_control)"
    printf 'mount_options=%s\n' "$current_options"
    printf 'membership=%s\n' "$current_membership"
  } > "$out/initial-cgroup-after.txt" || failed=1
  [[ $(< /sys/fs/cgroup/cgroup.subtree_control) == "$original_controllers" ]] || failed=1
  [[ $current_options == "$original_cgroup_options" ]] || failed=1
  [[ $current_membership == "$original_membership" ]] || failed=1
  [[ ! -e /sys/fs/cgroup/test ]] || failed=1
  if (( failed != 0 )); then
    printf 'Failed to restore the initial cgroup hierarchy state.\n' >&2
  fi
  return "$failed"
}
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
  if [[ -n $original_cgroup_options ]]; then
    restore_cgroup_state || cleanup_status=1
  fi
  chown -hR "$output_uid:$output_gid" -- "${outputs[@]}" || cleanup_status=1
  chown -h "$output_uid:$output_gid" -- "${test_directories[@]}" || cleanup_status=1
  if (( cleanup_status != 0 )); then
    printf 'Failed to restore the root test fixture.\n' >&2
    if (( status == 0 )); then status=1; fi
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ $initial_cgroup == true ]]; then
  original_controllers=$(< /sys/fs/cgroup/cgroup.subtree_control)
  original_membership=$(< /proc/self/cgroup)
  original_cgroup_options=$(findmnt --noheadings --first-only --target /sys/fs/cgroup --output FS-OPTIONS)
  [[ -n $original_cgroup_options ]]
  {
    printf 'cgroup_namespace=%s\n' "$(readlink /proc/self/ns/cgroup)"
    printf 'pid_namespace=%s\n' "$(readlink /proc/self/ns/pid)"
    printf 'controllers=%s\n' "$original_controllers"
    printf 'mount_options=%s\n' "$original_cgroup_options"
    printf 'membership=%s\n' "$original_membership"
  } | tee "$out/initial-cgroup-before.txt"
fi

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
