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

# Exercise the original smoke body in a caller-prepared user namespace.
# This deliberately does not test runsc's built-in namespace re-execution.
set -euo pipefail
out=${TEST_UNDECLARED_OUTPUTS_DIR:?}/rootless-podman
if [[ ${1:-} == --inside ]]; then
  shift
  [[ $(uname -m) == aarch64 && $(id -u) == 0 && $(id -g) == 0 ]]
  [[ $(readlink /proc/self/ns/user) != "$SMOKE_HOST_USERNS" ]]
  [[ $(readlink /proc/self/ns/mnt) != "$SMOKE_HOST_MOUNTNS" ]]
  {
    uname -a
    id
    cat /proc/self/attr/current
    grep -E '^(Uid|Gid|Cap|NoNewPrivs|Seccomp)' /proc/self/status
    readlink /proc/self/ns/user /proc/self/ns/mnt
  } | tee "$out/inside-identity.txt"
  for kind in uid gid; do
    if [[ $kind == uid ]]; then caller=$SMOKE_HOST_UID; else caller=$SMOKE_HOST_GID; fi
    cat "/proc/self/${kind}_map" | tee "$out/${kind}_map"
    awk -v caller="$caller" '
      $1 == 0 && $2 == caller && $3 == 1 { root = 1 }
      $1 > 0 && $3 > 1 { subordinate = 1 }
      END { exit !(root && subordinate) }
    ' "$out/${kind}_map"
  done
  # MaybeRunAsRoot returns without re-exec for exactly these capabilities:
  # CAP_SYS_ADMIN, CAP_SYS_CHROOT, CAP_SETUID and CAP_SETGID.
  effective=$(awk '$1 == "CapEff:" { print $2 }' /proc/self/status)
  (( (16#$effective & 0x2400c0) == 0x2400c0 ))
  printf 'SMOKE_CALLER_NAMESPACE_READY\n'
  printf 'SMOKE_COMMAND'; printf ' %q' "$@"; printf '\n'
  exec "$@"
fi

[[ $(uname -m) == aarch64 && $(id -u) != 0 && $# -gt 0 ]]
[[ $(command -v podman) == /usr/bin/podman ]]
[[ $(podman --version) == 'podman version 4.9.3' ]]
mkdir "$out"
{
  uname -a
  id
  cat /proc/self/attr/current
  grep -E '^(Uid|Gid|Cap|NoNewPrivs|Seccomp)' /proc/self/status
  readlink /proc/self/ns/user /proc/self/ns/mnt
  podman --version
  sha256sum /usr/bin/podman "$(realpath "$1")"
} | tee "$out/host-identity.txt"
policy=/proc/sys/kernel/apparmor_restrict_unprivileged_userns
cat "$policy" > "$out/apparmor-before.txt"
[[ $(<"$out/apparmor-before.txt") == 1 ]]
export SMOKE_HOST_UID SMOKE_HOST_GID SMOKE_HOST_USERNS SMOKE_HOST_MOUNTNS
SMOKE_HOST_UID=$(id -u)
SMOKE_HOST_GID=$(id -g)
SMOKE_HOST_USERNS=$(readlink /proc/self/ns/user)
SMOKE_HOST_MOUNTNS=$(readlink /proc/self/ns/mnt)
status=0
podman unshare "$(realpath "$0")" --inside "$@" > "$out/runtime.log" 2>&1 || status=$?
finished=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
printf 'SMOKE_CALLER_NAMESPACE exit=%s finished=%s\n' "$status" "$finished" | tee "$out/result.txt"
cat "$out/runtime.log"
cat "$policy" > "$out/apparmor-after.txt"
cmp "$out/apparmor-before.txt" "$out/apparmor-after.txt"
if (( status != 0 )); then exit "$status"; fi
grep -Fxq 'SMOKE_CALLER_NAMESPACE_READY' "$out/runtime.log"
grep -Fq 'Exiting with status: 0' "$out/runtime.log"
if grep -Fq '*** Re-running as root in new user namespace ***' "$out/runtime.log"; then
  printf 'Unexpected runsc namespace re-execution.\n' >&2
  exit 1
fi
