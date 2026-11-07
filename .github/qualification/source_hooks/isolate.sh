#!/bin/bash
# Only the namespace setup runs as root; the agent and both hooks run as the runner.
set -euo pipefail
w="$1"
uid="$2"
gid="$3"
variant="$4"
mount --make-rprivate /
mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /tmp
mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /run
mount --bind /proc/sys /proc/sys
mount -o remount,bind,ro /proc/sys
ip link set lo up
exec setpriv --reset-env --reuid="$uid" --regid="$gid" --clear-groups --no-new-privs \
  --bounding-set=-all --inh-caps=-all --ambient-caps=-all \
  /usr/bin/python3 -IB "$w/driver.py" "$variant"
