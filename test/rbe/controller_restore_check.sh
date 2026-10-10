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

# The native test shares the guest's hierarchy, even through another mount.
set -euo pipefail
[[ $EUID == 0 && $TEST_TARGET == *cgroup2_transfer_test_native_amd64 ]]
case "${CGROUP_CONTROLLER_STATE:?}" in
  enabled) operation=+ ;;
  disabled) operation=- ;;
  *) echo 'Expected enabled or disabled controller state' >&2; exit 1 ;;
esac
out=${TEST_UNDECLARED_OUTPUTS_DIR:?}/controller-state
mkdir -p "$out"
root=$(mktemp -d "${TEST_TMPDIR:?}/controller-check.XXXXXX")
mount -t cgroup2 none "$root"
control=$root/cgroup.subtree_control
original=$(< "$control")
# The EXIT trap invokes this function.
# shellcheck disable=SC2329
cleanup() {
  local result=$? cleanup_status=0 controller prefix
  trap - EXIT
  set +e
  for controller in pids memory; do
    prefix=-
    if [[ " $original " == *" $controller "* ]]; then prefix=+; fi
    if ! printf '%s\n' "$prefix$controller" > "$control"; then cleanup_status=1; fi
  done
  if ! cat "$control" > "$out/restored.txt"; then cleanup_status=1; fi
  if ! umount "$root"; then cleanup_status=1; fi
  if ! rmdir "$root"; then cleanup_status=1; fi
  if ! printf '%s\n' "$cleanup_status" > "$out/cleanup-exit.txt"; then cleanup_status=1; fi
  if (( result == 0 )); then result=$cleanup_status; fi
  exit "$result"
}
trap cleanup EXIT
printf '%s\n' "$original" > "$out/original.txt"
uname -a > "$out/kernel.txt"
cat "$root/cgroup.controllers" > "$out/available.txt"
for controller in pids memory; do
  grep -qw "$controller" "$out/available.txt"
  printf '%s\n' "$operation$controller" > "$control"
done
cat "$control" > "$out/before.txt"
status=0
"$@" || status=$?
cat "$control" > "$out/after.txt"
if ! cmp "$out/before.txt" "$out/after.txt"; then
  echo 'The controller-transfer suite changed subtree enablement' >&2
  status=1
fi
exit "$status"
