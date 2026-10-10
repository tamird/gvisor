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

# Fork-only diagnostic in an isolated test worker. Probe the documented n_tty
# callback arguments, not offsets into an unverified deployed kernel structure.
# https://github.com/torvalds/linux/blob/830b3c68c/Documentation/trace/kprobetrace.rst
set -euo pipefail
out="${TEST_UNDECLARED_OUTPUTS_DIR:?}/pty-kernel"
mkdir -p "$out"
tracefs=$(mktemp -d "${TEST_TMPDIR:?}/pty-tracefs.XXXXXX")
group="gvisor_pty_$$"
instance=
instance_created=false
mounted=false
events=()

finish() {
  local result=$? cleanup_status=0
  trap - EXIT
  set +e
  if [[ $instance_created == true ]]; then
    printf '0\n' > "$instance/tracing_on" || cleanup_status=1
    if [[ -e $instance/events/$group/enable ]]; then
      printf '0\n' > "$instance/events/$group/enable" || cleanup_status=1
    fi
    cat "$instance/trace" > "$out/trace.txt" || cleanup_status=1
    for stats in "$instance"/per_cpu/cpu*/stats; do
      [[ -f $stats ]] || continue
      printf '%s\n' "$stats" >> "$out/buffer-stats.txt"
      cat "$stats" >> "$out/buffer-stats.txt" || cleanup_status=1
    done
    rmdir "$instance" || cleanup_status=1
  fi
  if [[ $mounted == true ]]; then
    if (( ${#events[@]} > 0 )); then
      awk -v group="$group" 'index($1, group "_") == 1 {print}' "$tracefs/kprobe_profile" > "$out/probe-profile.txt" || cleanup_status=1
      [[ $(wc -l < "$out/probe-profile.txt") -eq ${#events[@]} ]] || cleanup_status=1
    fi
    for event in "${events[@]}"; do
      printf -- '-:%s/%s\n' "$group" "$event" >> "$tracefs/kprobe_events" || cleanup_status=1
    done
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
if [[ ! -e $tracefs/kprobe_events ]]; then
  printf '%s\n' 'The worker does not expose tracefs kprobe_events.' >&2
  exit 1
fi
[[ ! -e $tracefs/events/$group ]]
instance="$tracefs/instances/$group"
mkdir "$instance"
instance_created=true
printf '0\n' > "$instance/tracing_on"
printf '0\n' > "$instance/events/enable"
printf 'nop\n' > "$instance/current_tracer"
printf '256\n' > "$instance/buffer_size_kb"
printf 'mono\n' > "$instance/trace_clock"

add_probe() {
  local name="${group}_$1" kind=$2 symbol=$3 fields=$4 definition
  printf -v definition '%s:%s/%s %s %s' "$kind" "$group" "$name" "$symbol" "$fields"
  printf '%s\n' "$definition" >> "$tracefs/kprobe_events"
  events+=("$name")
  printf '%s\n' "$definition" >> "$out/probes.txt"
  cat "$tracefs/events/$group/$name/format" >> "$out/event-formats.txt"
}

add_probe receive p n_tty_receive_buf 'tty=$arg1:x64 first=+0($arg2):x8 count=$arg4:s32'
add_probe receive2 p n_tty_receive_buf2 'tty=$arg1:x64 first=+0($arg2):x8 count=$arg4:s32'
add_probe receive2_return r128 n_tty_receive_buf2 'result=$retval:s32'
add_probe read p n_tty_read 'tty=$arg1:x64 count=$arg4:u64'
add_probe read_return r128 n_tty_read 'result=$retval:s64'
add_probe poll p n_tty_poll 'tty=$arg1:x64'
add_probe poll_return r128 n_tty_poll 'mask=$retval:x32'
add_probe termios p n_tty_set_termios 'tty=$arg1:x64'
printf '1\n' > "$instance/events/$group/enable"
printf '1\n' > "$instance/tracing_on"

result=0
"$@" || result=$?
exit "$result"
