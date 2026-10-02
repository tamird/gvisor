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

out="${TEST_UNDECLARED_OUTPUTS_DIR:?}/pty-probe-registration"
mkdir "${out}"
trace_root=
instance=
mount_dir=
mounted=false
group="pty_registration_${BASHPID}"
probes=()
cleanup() {
  local status=$? cleanup_status=0 probe
  trap - EXIT
  set +e
  if [[ -n "${instance}" ]]; then
    for probe in "${probes[@]}"; do
      if [[ -e "${instance}/events/${group}/${probe}/enable" ]]; then
        printf '0\n' > "${instance}/events/${group}/${probe}/enable" || cleanup_status=1
      fi
    done
    rmdir "${instance}" || cleanup_status=1
  fi
  for probe in "${probes[@]}"; do
    if [[ -d "${trace_root}/events/${group}/${probe}" ]]; then
      printf -- '-:%s/%s\n' "${group}" "${probe}" >> "${trace_root}/dynamic_events" || cleanup_status=1
    fi
  done
  if (( ${#probes[@]} )); then
    awk -v group="${group}" 'index($1, ":" group "/")' "${trace_root}/dynamic_events" > "${out}/owned-events-after.txt" || cleanup_status=1
    [[ ! -s "${out}/owned-events-after.txt" ]] || cleanup_status=1
  fi
  if "${mounted}"; then umount "${mount_dir}" || cleanup_status=1; fi
  if [[ -n "${mount_dir}" ]]; then rmdir "${mount_dir}" || cleanup_status=1; fi
  printf 'registration_exit=%s cleanup_exit=%s\n' "${status}" "${cleanup_status}" > "${out}/completion.txt"
  if (( status == 0 && cleanup_status != 0 )); then status=${cleanup_status}; fi
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

missing() {
  printf 'PREREQUISITE_MISSING: %s\n' "$*" | tee "${out}/status.txt" >&2
  exit 2
}
error_log() {
  if [[ -r "${trace_root}/error_log" ]]; then
    cat "${trace_root}/error_log" > "${out}/error-log-$1.txt"
  else
    printf 'tracefs error_log is not readable\n' > "${out}/error-log-$1.txt"
  fi
}

id > "${out}/identity.txt"
uname -a > "${out}/uname.txt"
[[ $(id -u) == 0 ]] || missing "root in the isolated Firecracker worker"
for candidate in /sys/kernel/tracing /sys/kernel/debug/tracing; do
  if [[ -d "${candidate}/instances" && -w "${candidate}/instances" ]]; then
    trace_root=${candidate}
    break
  fi
done
if [[ -z "${trace_root}" ]]; then
  mount_dir=$(mktemp -d "${TEST_TMPDIR:?}/pty-probe-registration-tracefs.XXXXXX")
  mount -t tracefs tracefs "${mount_dir}" || missing "guest tracefs mount"
  mounted=true
  trace_root=${mount_dir}
fi
printf 'trace_root=%s owned_mount=%s group=%s\n' "${trace_root}" "${mounted}" "${group}" > "${out}/tracefs.txt"
[[ -r "${trace_root}/dynamic_events" && -w "${trace_root}/dynamic_events" ]] || missing "readable and writable dynamic_events"
[[ ! -e "${trace_root}/events/${group}" ]] || missing "unused event group"
private_instance="${trace_root}/instances/pty-registration-${BASHPID}"
mkdir "${private_instance}"
instance=${private_instance}
printf '0\n' > "${instance}/tracing_on"
error_log before
# The documented unified interface accepts the same definitions as
# kprobe_events. Register disabled events only; do not run the PTY case.
for spec in \
  'p:receive_entry n_tty_receive_buf2 tty=$arg1:x64 count=$arg4:s32' \
  'r64:receive_return n_tty_receive_buf2 consumed=$retval:s32' \
  'p:termios_entry n_tty_set_termios tty=$arg1:x64'; do
  kind=${spec%%:*}
  rest=${spec#*:}
  name=${group}_${rest%% *}
  definition="${kind}:${group}/${name} ${rest#* }"
  printf '%s\n' "${definition}" >> "${out}/definitions.txt"
  # Record ownership before the write so interruption cannot leak an event.
  probes+=("${name}")
  registration_status=0
  printf '%s\n' "${definition}" >> "${trace_root}/dynamic_events" 2>> "${out}/registration-errors.txt" || registration_status=$?
  printf '%s exit=%s\n' "${name}" "${registration_status}" >> "${out}/registration-status.txt"
  if (( registration_status != 0 )); then
    error_log after
    missing "dynamic registration rejected for ${name}"
  fi
  event="${instance}/events/${group}/${name}"
  cat "${event}/enable" > "${out}/${name}-enable.txt"
  [[ $(cat "${event}/enable") == 0 ]] || missing "event was not initially disabled"
  cat "${event}/format" > "${out}/${name}-format.txt"
done
error_log after
awk -v group="${group}" 'index($1, ":" group "/")' "${trace_root}/dynamic_events" > "${out}/owned-events.txt"
# Successful registration does not replace the absent return-miss counters.
printf 'kprobe_profile_exists=%s kprobe_profile_readable=%s\n' \
  "$([[ -e "${trace_root}/kprobe_profile" ]] && echo yes || echo no)" \
  "$([[ -r "${trace_root}/kprobe_profile" ]] && echo yes || echo no)" > "${out}/profile-access.txt"
printf 'DISABLED_PROBE_REGISTRATION_COMPLETE\n' | tee "${out}/status.txt"
