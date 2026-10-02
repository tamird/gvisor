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

set -euo pipefail

out="${TEST_UNDECLARED_OUTPUTS_DIR:?}/pty-probe-setup"
mkdir "${out}"
instance=
mount_dir=
mounted=false
cleanup() {
  local status=$? cleanup_status=0
  trap - EXIT
  set +e
  if [[ -n "${instance}" ]]; then
    rmdir "${instance}" || cleanup_status=1
  fi
  if "${mounted}"; then
    umount "${mount_dir}" || cleanup_status=1
  fi
  if [[ -n "${mount_dir}" ]]; then
    rmdir "${mount_dir}" || cleanup_status=1
  fi
  printf 'inspection_exit=%s cleanup_exit=%s\n' "${status}" "${cleanup_status}" > "${out}/completion.txt"
  if (( status == 0 && cleanup_status != 0 )); then status=${cleanup_status}; fi
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

id > "${out}/identity.txt"
uname -a > "${out}/uname.txt"
grep -E '^(Name|Pid|PPid|Uid|Gid|Groups|CapInh|CapPrm|CapEff|CapBnd|CapAmb|NoNewPrivs|Seccomp):' \
  "/proc/${BASHPID}/status" > "${out}/process-status.txt"
# Keep errno text as well as effective access tests; a failed compound test
# alone cannot distinguish absent files from permission restrictions.
inspect_path() {
  local path=$1 exists=no link=no readable=no writable=no
  [[ ! -e "${path}" ]] || exists=yes
  [[ ! -L "${path}" ]] || link=yes
  [[ ! -r "${path}" ]] || readable=yes
  [[ ! -w "${path}" ]] || writable=yes
  printf 'path=%s exists=%s symlink=%s readable=%s writable=%s\n' \
    "${path}" "${exists}" "${link}" "${readable}" "${writable}"
  stat --printf='type=%F mode=%a uid=%u gid=%g name=%N\n' -- "${path}" 2>&1 || printf 'stat_exit=%s\n' "$?"
}
trace_root=
for candidate in /sys/kernel/tracing /sys/kernel/debug/tracing; do
  inspect_path "${candidate}" >> "${out}/paths.txt"
  if [[ -z "${trace_root}" && -d "${candidate}/instances" && -w "${candidate}/instances" ]]; then
    trace_root=${candidate}
  fi
done
if [[ -z "${trace_root}" ]]; then
  mount_dir=$(mktemp -d "${TEST_TMPDIR:?}/pty-probe-setup-tracefs.XXXXXX")
  if mount -t tracefs tracefs "${mount_dir}" 2> "${out}/mount-error.txt"; then
    mounted=true
    trace_root=${mount_dir}
  else
    printf 'TRACEFS_MOUNT_FAILED\n' > "${out}/status.txt"
    exit 2
  fi
fi
printf 'trace_root=%s owned_mount=%s\n' "${trace_root}" "${mounted}" > "${out}/tracefs.txt"
awk '$0 ~ / - tracefs /' /proc/self/mountinfo > "${out}/tracefs-mounts.txt"
for name in kprobe_events kprobe_profile dynamic_events; do
  inspect_path "${trace_root}/${name}" >> "${out}/paths.txt"
done
private_instance="${trace_root}/instances/pty-probe-setup-${BASHPID}"
mkdir "${private_instance}"
instance=${private_instance}
inspect_path "${instance}" >> "${out}/paths.txt"
# Only /proc/config.gz is supplied by the running kernel; filesystem config
# candidates remain path-attributed. Do not substitute the published config.
release=$(uname -r)
for config in /proc/config.gz "/boot/config-${release}" "/lib/modules/${release}/build/.config"; do
  inspect_path "${config}" >> "${out}/config-paths.txt"
  if [[ -r "${config}" ]]; then
    printf 'source=%s\n' "${config}" >> "${out}/kernel-config.txt"
    if [[ "${config}" == *.gz ]]; then
      if command -v gzip > /dev/null; then
        gzip -dc -- "${config}" >> "${out}/kernel-config.txt"
      else
        printf 'gzip command unavailable\n' >> "${out}/kernel-config.txt"
      fi
    else
      cat "${config}" >> "${out}/kernel-config.txt"
    fi
  fi
done
printf 'SETUP_INSPECTION_COMPLETE\n' | tee "${out}/status.txt"
