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

# Diagnose the canonical smoke command without changing host policy or its
# exit status. A traced retry is diagnostic: ptrace can affect permission
# checks, so its result is never substituted for the ordinary execution.
set -euo pipefail
[[ $(uname -m) == aarch64 && $(id -u) != 0 && $# -gt 0 ]]
out=${TEST_UNDECLARED_OUTPUTS_DIR:?}/rootless-diagnostic
mkdir "$out"
started=$(date --iso-8601=seconds)
probe() {
  local name=$1 status=0
  shift
  "$@" > "$out/$name.log" 2>&1 || status=$?
  cat "$out/$name.log"
  printf 'PROBE name=%s exit=%s finished=%s\n' "$name" "$status" \
    "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" | tee -a "$out/results.txt"
}
status=0
"$@" > "$out/untraced.log" 2>&1 || status=$?
finished=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
cat "$out/untraced.log"
printf 'SMOKE_NATIVE exit=%s finished=%s\n' "$status" "$finished" | tee -a "$out/results.txt"
identity() {
  uname -a
  id
  cat /proc/self/attr/current
  grep -E '^(Uid|Gid|Cap|NoNewPrivs|Seccomp)' /proc/self/status
  readlink /proc/self/ns/user /proc/self/ns/mnt
  cat /proc/self/uid_map /proc/self/gid_map
  for path in /proc/sys/kernel/unprivileged_userns_clone \
    /proc/sys/kernel/apparmor_restrict_unprivileged_userns \
    /proc/sys/user/max_user_namespaces; do
    if [[ -r $path ]]; then printf '%s=' "$path"; cat "$path"; fi
  done
}
probe identity identity
binary=$(realpath "$1")
sha256sum "$binary" | tee "$out/binary.sha256"
probe binary-mount findmnt --target "$binary" --output TARGET,FSTYPE,OPTIONS
probe binary-path namei -l "$binary"
probe version "$1" --version
probe proc-self-exec bash -c 'exec /proc/self/exe -c true'
# These controls describe /usr/bin/unshare, whose distribution security
# profile can differ from runsc's; they cannot alone identify runsc's cause.
probe userns unshare --user --map-root-user -- sh -c \
  'id; cat /proc/self/attr/current /proc/self/uid_map /proc/self/gid_map'
probe user-mountns unshare --user --map-root-user --mount -- sh -c \
  'id; cat /proc/self/attr/current /proc/self/uid_map /proc/self/gid_map'
if (( status != 0 )) && command -v strace; then
  probe strace-version strace --version
  trace_status=0
  strace -f -qq -ttt -s 160 \
    -e trace=clone,clone3,unshare,execve,execveat,openat,write,setgroups,setresuid,setresgid,setuid,setgid,setsid,prctl \
    -o "$out/strace.log" "$@" > "$out/traced.log" 2>&1 || trace_status=$?
  cat "$out/traced.log"
  printf 'SMOKE_TRACED exit=%s finished=%s\n' "$trace_status" \
    "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" | tee -a "$out/results.txt"
elif (( status != 0 )); then
  printf 'SMOKE_TRACED unavailable=strace-not-installed\n' | tee -a "$out/results.txt"
fi
# Ordinary journal access is optional; no sudo or security-policy operation.
probe kernel-audit journalctl --dmesg --since "$started" --no-pager \
  --output=short-iso --grep='apparmor|userns|smoke_test|runsc'
if [[ -r /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]]; then
  cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns > "$out/apparmor-after.txt"
fi
exit "$status"
