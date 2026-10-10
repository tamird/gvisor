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

# Fork-only diagnostic in an isolated test worker. Function entries identify
# callbacks; syscall events record counts/results without private struct offsets.
# The function tracer supports private instances:
# https://github.com/torvalds/linux/blob/830b3c68c/kernel/trace/trace_functions.c#L436
set -euo pipefail
out="${TEST_UNDECLARED_OUTPUTS_DIR:?}/pty-kernel"
mkdir -p "$out"
tracefs=$(mktemp -d "${TEST_TMPDIR:?}/pty-tracefs.XXXXXX")
instance_name="gvisor_pty_$$"
instance=
instance_created=false
mounted=false

finish() {
  local result=$? cleanup_status=0
  trap - EXIT
  set +e
  if [[ $instance_created == true ]]; then
    printf '0\n' > "$instance/tracing_on" || cleanup_status=1
    printf '0\n' > "$instance/events/enable" || cleanup_status=1
    cat "$instance/trace" > "$out/trace.txt" || cleanup_status=1
    for stats in "$instance"/per_cpu/cpu*/stats; do
      [[ -f $stats ]] || continue
      printf '%s\n' "$stats" >> "$out/buffer-stats.txt"
      cat "$stats" >> "$out/buffer-stats.txt" || cleanup_status=1
    done
    rmdir "$instance" || cleanup_status=1
  fi
  if [[ $mounted == true ]]; then
    umount "$tracefs" || cleanup_status=1
  fi
  rmdir "$tracefs" || cleanup_status=1
  printf '%s\n' "$result" > "$out/primary-exit.txt" || cleanup_status=1
  printf '%s\n' "$cleanup_status" > "$out/cleanup-exit.txt" || cleanup_status=1
  if (( result == 0 )); then result=$cleanup_status; fi
  exit "$result"
}
trap finish EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

identity="${TEST_SRCDIR:?}/${TEST_WORKSPACE:?}/test/rbe/worker_identity"
[[ -x $identity ]]
"$identity" /bin/true
mount -t tracefs tracefs "$tracefs"
mounted=true
ls -l "$tracefs" > "$out/tracefs-files.txt"
cat "$tracefs/available_tracers" > "$out/available-tracers.txt"
if [[ -r $tracefs/available_filter_functions ]]; then
  awk '/n_tty_|flush_to_ldisc|tty_flip_buffer_push/ {print}' "$tracefs/available_filter_functions" > "$out/tty-filter-functions.txt"
fi
if [[ -r /proc/config.gz ]]; then
  gzip -dc /proc/config.gz > "$out/kernel.config"
fi
instance="$tracefs/instances/$instance_name"
mkdir "$instance"
instance_created=true
printf '0\n' > "$instance/tracing_on"
printf '0\n' > "$instance/events/enable"
printf 'nop\n' > "$instance/current_tracer"
printf '256\n' > "$instance/buffer_size_kb"
printf 'mono\n' > "$instance/trace_clock"

functions=(
  n_tty_receive_buf n_tty_receive_buf2 n_tty_receive_buf_common
  n_tty_receive_buf_standard n_tty_receive_char_special
  n_tty_read n_tty_poll n_tty_set_termios n_tty_kick_worker
  flush_to_ldisc tty_flip_buffer_push
)
printf '%s\n' "${functions[@]}" > "$out/requested-functions.txt"
cat "$out/requested-functions.txt" > "$instance/set_ftrace_filter"
cat "$instance/set_ftrace_filter" > "$out/function-filter.txt"
awk 'NR == FNR {want[$1] = 1; remaining++; next}
     {if (!want[$1]) bad = 1; else {delete want[$1]; remaining--}}
     END {exit (bad || remaining != 0)}' \
  "$out/requested-functions.txt" "$out/function-filter.txt"
for syscall in read write ioctl poll ppoll; do
  for phase in enter exit; do
    name="sys_${phase}_${syscall}"
    event="$instance/events/syscalls/$name"
    cat "$event/format" >> "$out/event-formats.txt"
    printf '1\n' > "$event/enable"
    [[ $(cat "$event/enable") == 1 ]]
    printf '%s\n' "$name" >> "$out/enabled-events.txt"
  done
done
printf 'function\n' > "$instance/current_tracer"
[[ $(cat "$instance/current_tracer") == function ]]
printf '1\n' > "$instance/tracing_on"

result=0
"$@" || result=$?
exit "$result"
