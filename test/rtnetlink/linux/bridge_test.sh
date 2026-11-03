#!/bin/bash

# Copyright 2024 The gVisor Authors.
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

set -xeo pipefail
fixture="$(dirname "$0")"
if [[ ! -f "$fixture/rtnetlink_test.sh" ]]; then
  fixture="$(dirname "$(readlink -f "$0")")"
fi
source "$fixture/rtnetlink_test.sh"
ip netns attach rootns "$$"
links=(bridge loopback)
closers=(client server)
if [[ -v TCP16908_FORWARD ]]; then
  [[ "$TCP16908_FORWARD" == 0 || "$TCP16908_FORWARD" == 1 ]]
  links=(bridge)
  closers=(server)
fi
for link in "${links[@]}"; do
for closer in "${closers[@]}"; do
ip link add br0 type bridge
ip netns add test0
ip netns add test1

ip link add hveth0 type veth peer name veth0 netns test0
ip link add hveth1 type veth peer name veth1 netns test1
ip link set up dev hveth0
ip link set up dev hveth1

if ip link set br0 master br0; then
  exit 1
fi

ip link set hveth0 master br0
ip link show master br0 | grep "hveth0" || exit 1
ip link set hveth1 master br0
ip link show master br0 | grep "hveth1" || exit 1

ip addr add 192.168.0.3/24 dev br0
ip link set up dev br0
ip netns exec test0 ip link set up dev veth0
ip netns exec test1 ip link set up dev veth1
ip netns exec test0 ip addr add 192.168.0.1/24 dev veth0
ip netns exec test1 ip addr add 192.168.0.2/24 dev veth1

ip link set br0 mtu 1460
ip link set hveth0 mtu 1460
ip link set hveth1 mtu 1460
ip -n test0 link set veth0 mtu 1460
ip -n test1 link set veth1 mtu 1460
ip -n test1 link set lo up
if [[ -v TCP16908_FORWARD ]]; then
  printf '%s\n' "$TCP16908_FORWARD" > /proc/sys/net/ipv4/ip_forward
  [[ "$(cat /proc/sys/net/ipv4/ip_forward)" == "$TCP16908_FORWARD" ]]
fi
python3 "$fixture/short_connections.py" run "$link" "$closer"

# Destroy namespaces and the bridge.
ip netns del test0
ip link del br0
ip netns del test1
if ! wait_for ! ip link show hveth0 2>/dev/null; then
  fail "hveth0 hasn't been destroyed"
fi
if ! wait_for ! ip link show hveth1 2>/dev/null; then
  fail "hveth1 hasn't been destroyed"
fi
if ! wait_for ! ip link show br0 2>/dev/null; then
  fail "br0 hasn't been destroyed"
fi
done
done
