#!/usr/bin/env bash
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

# Read the initial guest hierarchy without mounting filesystems, enabling
# controllers, or starting Docker. A v2 mount alone does not establish whether
# its controllers are in use and therefore unavailable to a v1 fixture.
set -euo pipefail

[[ "$(id -u)" == 0 ]]
printf 'Kernel: '
uname -rm
printf '\nCgroup-related boot arguments:\n'
awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^(cgroup_|systemd\.(unified_cgroup_hierarchy|legacy_systemd_cgroup_controller)=)/) print $i }' /proc/cmdline
printf '\nController registration (/proc/cgroups):\n'
cat /proc/cgroups

for pid in self 1; do
  printf '\n/proc/%s/cgroup:\n' "${pid}"
  cat "/proc/${pid}/cgroup"
  printf '\n/proc/%s/mountinfo (cgroups only):\n' "${pid}"
  awk '{ for (i = 1; i < NF; i++) if ($i == "-" && ($(i+1) == "cgroup" || $(i+1) == "cgroup2")) print }' "/proc/${pid}/mountinfo"
  for namespace in mnt cgroup pid; do
    printf '/proc/%s/ns/%s: ' "${pid}" "${namespace}"
    readlink "/proc/${pid}/ns/${namespace}"
  done
done

printf '\nPID 1 executable name: '
cat /proc/1/comm
printf '\n/sys/fs/cgroup filesystem: '
stat -f -c '%T' /sys/fs/cgroup
for name in cgroup.controllers cgroup.subtree_control cgroup.type cgroup.events; do
  printf '\n/sys/fs/cgroup/%s:\n' "${name}"
  if [[ -e "/sys/fs/cgroup/${name}" ]]; then
    cat "/sys/fs/cgroup/${name}"
  else
    printf 'Not present\n'
  fi
done
