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

# Used only inside Bazel's privileged Docker sandbox, never on its host.
set -euo pipefail
[[ $EUID == 0 && $# != 0 ]]
container_cgroup_ns=$(readlink /proc/self/ns/cgroup)
container_pid_ns=$(readlink /proc/self/ns/pid)
[[ $container_cgroup_ns != "${GVISOR_HOST_CGROUP_NS:?}" ]]
[[ $container_pid_ns != "${GVISOR_HOST_PID_NS:?}" ]]
[[ $(< /proc/self/cgroup) == '0::/' ]]
[[ $(stat -f -c %T /sys/fs/cgroup) == cgroup2fs ]]

if [[ -n ${GVISOR_DOCKER_NETWORK:-} ]]; then
  # Bazel only offers none or host networking. Attach this exact sandbox to
  # the job-owned bridge before its private daemon starts, then remove access
  # to the host Docker API before executing any test code.
  socket=/run/gvisor-host-docker.sock
  [[ -S $socket && $GVISOR_DOCKER_NETWORK =~ ^[0-9a-f]{64}$ ]]
  host_docker=(docker --host="unix://$socket")
  hostname=$(hostname)
  [[ $hostname =~ ^[0-9a-f]{12}$ ]]
  read -r container_id container_hostname command_id network_mode privileged < <(
    "${host_docker[@]}" inspect --type=container --format \
      '{{.Id}} {{.Config.Hostname}} {{index .Config.Labels "command_id"}} {{.HostConfig.NetworkMode}} {{.HostConfig.Privileged}}' "$hostname"
  )
  [[ $container_id =~ ^[0-9a-f]{64}$ && ${container_id:0:12} == "$hostname" ]]
  [[ $container_hostname == "$hostname" && $command_id =~ ^[0-9a-f-]{36}$ ]]
  [[ $network_mode == none && $privileged == true ]]
  network_ns=$(readlink /proc/self/ns/net)
  [[ $network_ns != "${GVISOR_HOST_NET_NS:?}" ]]
  [[ $("${host_docker[@]}" exec "$container_id" readlink /proc/self/ns/net) == "$network_ns" ]]
  [[ $("${host_docker[@]}" network inspect --format '{{.Driver}} {{.Internal}}' "$GVISOR_DOCKER_NETWORK") == 'bridge false' ]]
  "${host_docker[@]}" network disconnect none "$container_id"
  "${host_docker[@]}" network connect "$GVISOR_DOCKER_NETWORK" "$container_id"
  [[ $(readlink /proc/self/ns/net) == "$network_ns" ]]
  printf 'Attached Docker sandbox %s to job bridge %s in %s.\n' "$container_id" "$GVISOR_DOCKER_NETWORK" "$network_ns"
  umount "$socket"
  [[ ! -S $socket ]]
  unset host_docker GVISOR_DOCKER_NETWORK
  printf 'Host Docker socket removed before test execution.\n'
  cat /proc/net/route /etc/resolv.conf
fi

# Keep overlay2's writable layers off the outer container's overlay filesystem,
# which rejected their mount. Bazel mounts TEST_TMPDIR from the host disk;
# expose it at /tmp to retain the private daemon's short socket paths.
mount --bind "${TEST_TMPDIR:?}" /tmp
stat -f -c 'Docker scratch filesystem: %T' /tmp

# A cgroup namespace's root is still a non-root cgroup in the host hierarchy.
# Its controllers cannot serve Docker's children while test processes occupy it.
# https://github.com/torvalds/linux/blob/2d8a435cb/Documentation/admin-guide/cgroup-v2.rst#L510-L534
leaf=/sys/fs/cgroup/test-runner
mkdir "$leaf"
printf '1\n' > "$leaf/cgroup.procs"
printf '%s\n' "$$" > "$leaf/cgroup.procs"

# Move Bazel's setup/watchdog too, so future children inherit the leaf. Drain
# children forked during migration, then require an empty parent before testing.
# The outer Docker lifecycle removes this leaf with the test's container.
for ((attempt = 0; attempt < 8; attempt++)); do
  mapfile -t processes < /sys/fs/cgroup/cgroup.procs
  if (( ${#processes[@]} == 0 )); then
    break
  fi
  for pid in "${processes[@]}"; do
    if ! printf '%s\n' "$pid" > "$leaf/cgroup.procs"; then
      # A short-lived watchdog child may exit between enumeration and the move.
      if [[ -d /proc/$pid ]]; then
        printf 'Could not move live process %s out of the cgroup root.\n' "$pid" >&2
        exit 1
      fi
    fi
  done
done
mapfile -t processes < /sys/fs/cgroup/cgroup.procs
if (( ${#processes[@]} != 0 )); then
  printf 'Processes remain in the delegated cgroup root.\n' >&2
  exit 1
fi
printf 'Delegated cgroup root is empty; test membership: %s\n' "$(< /proc/self/cgroup)"
exec "$@"
