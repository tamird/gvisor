#!/bin/bash
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
if [[ ${1:-} != --inside ]]; then
  printf '%s\n' "$(id -u)" > "$MTU_WORK/outer-uid"
  exec unshare --user --map-root-user --net --fork "$0" --inside "$@"
fi
shift
test "$(id -u)" = 0
# Enumerate through rtnetlink in this namespace. The inherited sysfs mount can
# still show the parent namespace and is never used to select a device.
ip -o link show > "$MTU_WORK/initial-links"
test "$(wc -l < "$MTU_WORK/initial-links")" -eq 1
grep -q ' lo:' "$MTU_WORK/initial-links"
ip link set lo up
ip link add mtua address 02:00:00:00:71:01 type veth peer name mtub address 02:00:00:00:71:02
for iface in mtua mtub; do
  ip link set dev "$iface" mtu 1500 up
  if command -v ethtool >/dev/null; then
    for flag in tx sg tso gso gro lro; do
      ethtool -K "$iface" "$flag" off >> "$MTU_WORK/offload-setup" 2>&1 || true
    done
    ethtool -k "$iface" > "$MTU_WORK/$iface.features"
  fi
done
test -z "$(ip route show default)"
test -z "$(ip -6 route show default)"
ip -o link show > "$MTU_WORK/links"
cat /proc/self/uid_map > "$MTU_WORK/uid_map"
export GSO_PRIVATE_WIRE=1
if command -v strace >/dev/null && strace -qq -e trace=writev -o "$MTU_WORK/trace-preflight" -- /bin/true; then
  exec strace -ff -qq -ttt -s 16 -xx -e trace=sendmmsg,writev -o "$MTU_WORK/syscall" -- "$@"
fi
exec "$@"
