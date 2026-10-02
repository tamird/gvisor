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

out="${TEST_UNDECLARED_OUTPUTS_DIR:?}/tcp-timestamp-trace"
mkdir "${out}"
instance=
mount_dir=
mounted=false
trace_started=false
events=(
  workqueue/workqueue_queue_work
  workqueue/workqueue_execute_start
  workqueue/workqueue_execute_end
  syscalls/sys_enter_setsockopt
  syscalls/sys_exit_setsockopt
  syscalls/sys_enter_write
  syscalls/sys_exit_write
  syscalls/sys_enter_recvmsg
  syscalls/sys_exit_recvmsg
  sched/sched_process_exec
)

cleanup() {
  local status=$? cleanup_status=0 event stats
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
      # Overflow makes causal ordering incomplete, even if the case passes.
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
    rmdir "${instance}" || cleanup_status=1
  fi
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
trace_root=
for candidate in /sys/kernel/tracing /sys/kernel/debug/tracing; do
  if [[ -d "${candidate}/instances" && -w "${candidate}/instances" ]]; then
    trace_root=${candidate}
    break
  fi
done
if [[ -z "${trace_root}" ]]; then
  command -v mount > /dev/null && command -v umount > /dev/null || missing "guest mount/umount commands"
  mount_dir=$(mktemp -d "${TEST_TMPDIR:?}/tcp-timestamp-tracefs.XXXXXX")
  mount -t tracefs tracefs "${mount_dir}" || missing "guest tracefs mount denied or unsupported"
  mounted=true
  trace_root=${mount_dir}
fi
printf 'trace_root=%s owned_mount=%s\n' "${trace_root}" "${mounted}" > "${out}/tracefs.txt"
# Never change the global tracer or an existing instance.
private_instance="${trace_root}/instances/tcp-timestamp-${BASHPID}"
mkdir "${private_instance}" || missing "private trace instance creation"
instance=${private_instance}
printf '0\n' > "${instance}/tracing_on" || missing "private tracing control"
awk '$3 == "netstamp_clear" || $3 == "net_enable_timestamp"' /proc/kallsyms > "${out}/symbols.txt" || missing "readable /proc/kallsyms"
[[ $(wc -l < "${out}/symbols.txt") == 2 ]] || missing "netstamp_clear and net_enable_timestamp symbols"
cat "${instance}/trace_clock" > "${out}/available-clocks.txt" || missing "readable trace clock"
grep -qw global "${instance}/trace_clock" || missing "cross-CPU global trace clock"
printf 'global\n' > "${instance}/trace_clock" || missing "selecting global trace clock"
cat "${instance}/trace_clock" > "${out}/selected-clock.txt" || missing "selected trace clock readback"
# One short case; retain bounded per-CPU buffers and report any loss.
printf '256\n' > "${instance}/buffer_size_kb" || missing "private trace buffer configuration"
for event in "${events[@]}"; do
  [[ -r "${instance}/events/${event}/format" && -w "${instance}/events/${event}/enable" ]] || missing "trace event ${event}"
  cat "${instance}/events/${event}/format" > "${out}/${event//\//-}-format.txt" || missing "readable event format ${event}"
  printf '1\n' > "${instance}/events/${event}/enable" || missing "enabling event ${event}"
done
# Discovery and execution keep the owning native runner's normal contract.
# No extra operation is inserted between the test's setsockopt/write/recvmsg.
export TESTBRIDGE_TEST_ONLY=AllInetTests/TcpSocketTest.TcpSCMPriority/0
printf '1\n' > "${instance}/tracing_on" || missing "starting private tracing"
trace_started=true
printf 'TRACE_READY\n' | tee "${out}/status.txt"
case_status=0
"$1" || case_status=$?
printf '%s\n' "${case_status}" > "${out}/case-exit.txt"
exit "${case_status}"
