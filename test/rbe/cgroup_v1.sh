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

# Controller bindings are VM-wide even in a private mount namespace. This
# wrapper requires a disposable remote VM and initially unused controllers.
set -euo pipefail
out=${TEST_UNDECLARED_OUTPUTS_DIR:?}
controllers=(cpuset cpu cpuacct blkio memory devices freezer net_cls perf_event net_prio hugetlb pids)

if [[ ${1:-} != --mounts ]]; then
  test "$#" -gt 0
  test "$(id -u)" -eq 0
  test "$(stat -f -c %T /sys/fs/cgroup)" = cgroup2fs
  test "$(cat /proc/self/cgroup)" = '0::/'
  test "$(cat /proc/1/cgroup)" = '0::/'
  awk '$4 == "/" && $5 == "/sys/fs/cgroup" && / - cgroup2 / { found = 1 } END { exit !found }' /proc/self/mountinfo
  test -z "$(cat /sys/fs/cgroup/cgroup.subtree_control)"
  test "$(cat /sys/fs/cgroup/cgroup.controllers)" = 'cpuset cpu io memory hugetlb pids'
  test "$(awk 'NR > 1 { printf "%s%s", sep, $1; sep = " " }' /proc/cgroups)" = "${controllers[*]}"
  awk 'NR > 1 && ($2 != 0 || $3 != 1 || $4 != 1) { exit 1 }' /proc/cgroups
  cat /proc/cgroups > "${out}/cgroups-before.txt"
  cat /sys/fs/cgroup/cgroup.controllers > "${out}/controllers-before.txt"
  cat /sys/fs/cgroup/cgroup.subtree_control > "${out}/subtree-before.txt"
  awk '/ - cgroup2? /' /proc/self/mountinfo > "${out}/mounts-before.txt"

  status=0
  unshare --mount --propagation private "$0" --mounts "$@" || status=$?

  cat /proc/cgroups > "${out}/cgroups-after.txt"
  cat /proc/self/cgroup > "${out}/self-membership-after.txt"
  cat /proc/1/cgroup > "${out}/init-membership-after.txt"
  cat /sys/fs/cgroup/cgroup.controllers > "${out}/controllers-after.txt"
  cat /sys/fs/cgroup/cgroup.subtree_control > "${out}/subtree-after.txt"
  awk '/ - cgroup2? /' /proc/self/mountinfo > "${out}/mounts-after.txt"
  cmp "${out}/subtree-before.txt" "${out}/subtree-after.txt"
  cmp "${out}/mounts-before.txt" "${out}/mounts-after.txt"
  # Dead cgroup references can outlive rmdir and unmount. Global bindings belong
  # to this disposable VM, not to a reusable worker or the coordinator.
  # https://github.com/torvalds/linux/blob/830b3c68c/kernel/cgroup/cgroup.c#L5840
  printf 'Controller bindings before VM disposal:\n'
  cat "${out}/cgroups-after.txt" "${out}/self-membership-after.txt"
  exit "${status}"
fi
shift

mounted=()
overlay=false
child=
cleanup() {
  status=$?
  trap - EXIT
  set +e
  if [[ -n ${child} ]]; then
    # unshare --fork ignores TERM/INT while waiting. Its --kill-child option
    # kills namespace init when this exact, owned wrapper process dies.
    kill -KILL "${child}"
    wait "${child}"
    status=1
  fi
  cleanup_deadline=$((SECONDS + 15))
  for ((i = ${#mounted[@]} - 1; i >= 0; i--)); do
    # All descendants were created in these newly mounted hierarchies. The
    # kernel rejects rmdir of populated groups; never move or kill their tasks.
    # Killing the unshare waiter can precede its child's namespace teardown.
    until find "${mounted[i]}" -depth -mindepth 1 -type d -exec rmdir -- {} + \
        2> "${out}/rmdir-errors.txt"; do
      if (( SECONDS >= cleanup_deadline )); then
        cat "${out}/rmdir-errors.txt" >&2
        status=1
        break
      fi
      sleep 0.1
    done
    umount "${mounted[i]}" || status=1
  done
  if ${overlay}; then
    umount /sys/fs/cgroup || status=1
  fi
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Recheck immediately before acquiring VM-wide controller bindings.
cmp /proc/cgroups "${out}/cgroups-before.txt"
cmp /sys/fs/cgroup/cgroup.subtree_control "${out}/subtree-before.txt"
mount -t tmpfs -o nosuid,nodev,noexec cgroup-v1-fixture /sys/fs/cgroup
overlay=true
for controller in "${controllers[@]}"; do
  path=/sys/fs/cgroup/${controller}
  mkdir "${path}"
  mount -t cgroup -o "${controller}" "${controller}" "${path}"
  mounted+=("${path}")
done
# Match the legacy hierarchy layout; this is not a systemd host.
mkdir /sys/fs/cgroup/systemd
mount -t cgroup -o none,name=systemd systemd /sys/fs/cgroup/systemd
mounted+=(/sys/fs/cgroup/systemd)
cat /proc/cgroups /proc/self/cgroup > "${out}/v1-membership.txt"
awk '/ - cgroup2? /' /proc/self/mountinfo > "${out}/v1-mounts.txt"
cat /sys/fs/cgroup/cpuset/cpuset.cpus /sys/fs/cgroup/cpuset/cpuset.mems

# Keep the command and its children in one PID view. Namespace-init exit
# contains even children in other process groups; the caller owns test timeouts.
# https://github.com/torvalds/linux/blob/830b3c68c/kernel/pid_namespace.c#L166
unshare --pid --fork --kill-child --mount-proc "$@" &
child=$!
status=0
wait "${child}" || status=$?
child=
exit "${status}"
