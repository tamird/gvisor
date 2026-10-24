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

# Fork-only capability check for the changing-RTT pilot's kernel operations.
set -euo pipefail

run() {
  printf 'OPERATION'
  printf ' %q' "$@"
  printf '\n'
  local status=0
  "$@" || status=$?
  if (( status != 0 )); then
    printf 'FIRST_FAILED_OPERATION exit=%s argv=' "$status"
    printf ' %q' "$@"
    printf '\n'
    exit "$status"
  fi
}

if [[ ${1:-} != --inner ]]; then
  printf 'HOST\n'
  uname -a
  id
  for tool in bash unshare nsenter mount umount ip tc sha256sum; do
    if ! command -v "$tool"; then
      printf 'MISSING_USERLAND tool=%s; no kernel result for this operation\n' "$tool"
      exit 127
    fi
    sha256sum "$(command -v "$tool")"
  done
  run ip -Version
  run tc -Version
  # A built-in qdisc needs no module. Always attempt the actual tc operation.
  if command -v modprobe; then
    status=0
    modprobe sch_netem || status=$?
    printf 'MODPROBE_OBSERVATION exit=%s (not a capability verdict)\n' "$status"
  else
    printf 'MODPROBE_OBSERVATION unavailable (not a capability verdict)\n'
  fi
  [[ $(id -u) == 0 ]]
  script=$(readlink -f "$0")
  run unshare --mount --net --pid --fork --mount-proc /bin/bash "$script" --inner
  printf 'HOSTED_TCP_TOPOLOGY_PASS\n'
  exit 0
fi

# All links, bridges, mounts and network namespace handles are private to this
# process tree. Exiting the PID namespace also reaps interrupted children.
run mount --make-rprivate /
probe_dir=$(mktemp -d "${TEST_TMPDIR:?}/tcp-topology.XXXXXX")
bridge_devices=()
cleanup() {
  local status=$? cleanup_status=0
  set +e
  trap - EXIT
  for device in "${bridge_devices[@]}"; do
    ip link set "$device" nomaster || cleanup_status=1
  done
  for endpoint in client client2 server; do
    if [[ -e $probe_dir/$endpoint.netns ]]; then
      umount "$probe_dir/$endpoint.netns" || cleanup_status=1
    fi
  done
  rm -rf -- "$probe_dir" || cleanup_status=1
  printf 'TOPOLOGY_CLEANUP exit=%s\n' "$cleanup_status"
  if (( status == 0 )); then status=$cleanup_status; fi
  exit "$status"
}
trap cleanup EXIT
run ip link add server.0 type veth peer name server.1
run ip link add wan.0 type veth peer name wan.1
run ip link add name br0 type bridge
run ip link add name br1 type bridge
run ip link set server.1 master br1
bridge_devices+=(server.1)
run ip link set wan.0 master br0
bridge_devices+=(wan.0)
run ip link set wan.1 master br1
bridge_devices+=(wan.1)
for device in wan.0 wan.1 br0 br1 server.1; do
  run ip link set "$device" up
  run ip link set "$device" mtu 1500
done
for endpoint in client client2 server; do
  if [[ $endpoint != server ]]; then
    run ip link add "$endpoint.0" type veth peer name "$endpoint.1"
    run ip link set "$endpoint.1" master br0
    bridge_devices+=("$endpoint.1")
    run ip link set "$endpoint.1" up
  fi
  touch "$probe_dir/$endpoint.netns"
  run unshare -n mount --bind /proc/self/ns/net "$probe_dir/$endpoint.netns"
  run ip link set dev "$endpoint.0" netns "$probe_dir/$endpoint.netns"
  run nsenter --net="$probe_dir/$endpoint.netns" ip link set "$endpoint.0" mtu 1500
  run nsenter --net="$probe_dir/$endpoint.netns" ip link set "$endpoint.0" up
  run nsenter --net="$probe_dir/$endpoint.netns" ip link set lo up
done
# These are the exact no-loss WAN parameters and two changes from the pilot;
# this short probe does not execute the original 60/420-second schedule.
for device in wan.0 wan.1; do
  run tc qdisc add dev "$device" root netem limit 100 delay 5ms rate 20mbit
done
for delay in 5 50 5; do
  for device in wan.0 wan.1; do
    run tc qdisc change dev "$device" root netem limit 100 delay "${delay}ms" rate 20mbit
    run tc -s -d qdisc show dev "$device"
  done
done
run ip -d link show
