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

out="${TEST_UNDECLARED_OUTPUTS_DIR:?}/pty-input-trace"
mkdir "${out}"
instance=
mount_dir=
mounted=false
trace_started=false
trace_root=
probe_group="pty_${BASHPID}"
probes=()
events=(
  syscalls/sys_enter_ioctl
  syscalls/sys_exit_ioctl
  syscalls/sys_enter_write
  syscalls/sys_exit_write
  syscalls/sys_enter_read
  syscalls/sys_exit_read
  syscalls/sys_enter_poll
  syscalls/sys_exit_poll
  sched/sched_process_exec
)

cleanup() {
  local status=$? cleanup_status=0 event stats probe
  trap - EXIT
  set +e
  if [[ -n "${instance}" ]]; then
    printf '0\n' > "${instance}/tracing_on" || cleanup_status=1
    if "${trace_started}"; then
      cat "${instance}/trace" > "${out}/trace.txt" || cleanup_status=1
      for stats in "${instance}"/per_cpu/cpu*/stats; do
        printf '%s\n' "${stats##*/per_cpu/}"
        cat "${stats}" || cleanup_status=1
      done > "${out}/trace-stats.txt"
      awk '/^(overrun|commit overrun|dropped events):/ {
             seen++
             if ($NF !~ /^[0-9]+$/ || $NF != 0) invalid=1
           }
           END {exit !seen || invalid}' "${out}/trace-stats.txt" || cleanup_status=1
    fi
    for event in "${events[@]}"; do
      if [[ -e "${instance}/events/${event}/enable" ]]; then
        printf '0\n' > "${instance}/events/${event}/enable" || cleanup_status=1
      fi
    done
    if "${trace_started}"; then
      # The kernel profile prints event names, without their group. Names are
      # unique too, so this reads only the probes acquired by this action.
      awk -v prefix="${probe_group}_" 'index($1, prefix) == 1' \
        "${trace_root}/kprobe_profile" > "${out}/kprobe-profile.txt" || cleanup_status=1
      awk -v prefix="${probe_group}_" '
        $1 == prefix "receive_entry" || $1 == prefix "receive_return" ||
        $1 == prefix "termios_entry" {
          names[$1]++
          seen++
          if (NF != 3 || $2 !~ /^[0-9]+$/ || $3 !~ /^[0-9]+$/ || $3 != 0) invalid=1
        }
        END {
          for (name in names) if (names[name] != 1) invalid=1
          exit seen != 3 || invalid
        }' "${out}/kprobe-profile.txt" || cleanup_status=1
    fi
    rmdir "${instance}" || cleanup_status=1
  fi
  # Registration is append-only; delete exactly the events we registered.
  for probe in "${probes[@]}"; do
    printf -- '-:%s/%s\n' "${probe_group}" "${probe}" >> "${trace_root}/kprobe_events" || cleanup_status=1
  done
  if "${mounted}"; then
    umount "${mount_dir}" || cleanup_status=1
  fi
  if [[ -n "${mount_dir}" ]]; then
    rmdir "${mount_dir}" || cleanup_status=1
  fi
  printf 'original_exit=%s cleanup_exit=%s\n' "${status}" "${cleanup_status}" > "${out}/completion.txt"
  if (( status == 0 && cleanup_status != 0 )); then
    status=${cleanup_status}
  fi
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

missing() {
  printf 'PREREQUISITE_MISSING: %s\n' "$*" | tee "${out}/status.txt" >&2
  exit 2
}

[[ $# == 1 && -x "$1" ]] || missing "declared native runner executable"
[[ $(id -u) == 0 ]] || missing "root in the isolated Firecracker worker"
uname -a > "${out}/uname.txt"
for candidate in /sys/kernel/tracing /sys/kernel/debug/tracing; do
  if [[ -d "${candidate}/instances" && -w "${candidate}/instances" ]]; then
    trace_root=${candidate}
    break
  fi
done
if [[ -z "${trace_root}" ]]; then
  command -v mount > /dev/null && command -v umount > /dev/null || missing "guest mount/umount commands"
  mount_dir=$(mktemp -d "${TEST_TMPDIR:?}/pty-input-tracefs.XXXXXX")
  mount -t tracefs tracefs "${mount_dir}" || missing "guest tracefs mount denied or unsupported"
  mounted=true
  trace_root=${mount_dir}
fi
printf 'trace_root=%s owned_mount=%s probe_group=%s\n' "${trace_root}" "${mounted}" "${probe_group}" > "${out}/tracefs.txt"
private_instance="${trace_root}/instances/pty-input-${BASHPID}"
mkdir "${private_instance}" || missing "private trace instance creation"
instance=${private_instance}
printf '0\n' > "${instance}/tracing_on" || missing "private tracing control"
awk '$3 == "n_tty_receive_buf2" || $3 == "n_tty_set_termios"' /proc/kallsyms > "${out}/symbols.txt" || missing "readable /proc/kallsyms"
[[ $(wc -l < "${out}/symbols.txt") == 2 ]] || missing "n_tty_receive_buf2 and n_tty_set_termios symbols"
[[ -w "${trace_root}/kprobe_events" && -r "${trace_root}/kprobe_profile" ]] || missing "dynamic kprobe registration and profile access"
[[ ! -e "${trace_root}/events/${probe_group}" ]] || missing "unused probe group"
# No struct offsets or payload dereferences: record the public callback's
# tty identity, offered count and consumed return value. A positive return
# means processing, not necessarily storage by the canonical line discipline.
for spec in \
  'p:receive_entry n_tty_receive_buf2 tty=$arg1:x64 count=$arg4:s32' \
  'r64:receive_return n_tty_receive_buf2 consumed=$retval:s32' \
  'p:termios_entry n_tty_set_termios tty=$arg1:x64'; do
  kind=${spec%%:*}
  rest=${spec#*:}
  name=${probe_group}_${rest%% *}
  printf '%s:%s/%s %s\n' "${kind}" "${probe_group}" "${name}" "${rest#* }" >> "${trace_root}/kprobe_events" || missing "registering probe ${name}"
  probes+=("${name}")
  events+=("${probe_group}/${name}")
done
cat "${instance}/trace_clock" > "${out}/available-clocks.txt" || missing "readable trace clock"
grep -qw global "${instance}/trace_clock" || missing "cross-CPU global trace clock"
printf 'global\n' > "${instance}/trace_clock" || missing "selecting global trace clock"
cat "${instance}/trace_clock" > "${out}/selected-clock.txt" || missing "selected trace clock readback"
# Bound each action's trace; loss or missed return probes invalidate ordering.
printf '512\n' > "${instance}/buffer_size_kb" || missing "private trace buffer configuration"
for event in "${events[@]}"; do
  [[ -r "${instance}/events/${event}/format" && -w "${instance}/events/${event}/enable" ]] || missing "trace event ${event}"
  cat "${instance}/events/${event}/format" > "${out}/${event//\//-}-format.txt" || missing "readable event format ${event}"
  printf '1\n' > "${instance}/events/${event}/enable" || missing "enabling event ${event}"
done
export TESTBRIDGE_TEST_ONLY=PtyTest.SwitchNoncanonToCanonNewlineBig
printf '1\n' > "${instance}/tracing_on" || missing "starting private tracing"
trace_started=true
printf 'CASE_BEGIN\n' > "${instance}/trace_marker" || missing "private case marker"
printf 'TRACE_READY\n' | tee "${out}/status.txt"
case_status=0
"$1" || case_status=$?
printf '%s\n' "${case_status}" > "${out}/case-exit.txt"
marker_status=0
printf 'CASE_END\n' > "${instance}/trace_marker" || marker_status=$?
if (( case_status == 0 && marker_status != 0 )); then
  case_status=${marker_status}
fi
exit "${case_status}"
