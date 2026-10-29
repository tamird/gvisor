#!/bin/bash
set -euo pipefail
test "$(cat "$FENCE_HOST_PROBE")" = "$FENCE_HOST_ID"
for tool in x86_64-linux-gnu-gcc aarch64-linux-gnu-gcc aarch64-linux-gnu-ld ip unshare; do
  command -v "$tool"
done
for arch in x86_64 aarch64; do
  object="$TEST_TMPDIR/$arch.o"
  printf 'int witness(void) { return 0; }\n' | "/usr/bin/$arch-linux-gnu-gcc" -c -x c - -o "$object"
  case $arch in x86_64) machine=X86-64;; aarch64) machine=AArch64;; esac
  readelf -h "$object" | grep -F "$machine"
done
# The packetimpact runner creates a user/network namespace, veth endpoints,
# and disables duplicate-address detection before either peer is started.
unshare --user --map-root-user --net sh -ec '
  ip link add tb-0-ctrl type veth peer name tb-0-peer
  printf 0 > /proc/sys/net/ipv6/conf/tb-0-ctrl/accept_dad
  test "$(cat /proc/sys/net/ipv6/conf/tb-0-ctrl/accept_dad)" = 0
'
echo FENCE_HOST_OK
