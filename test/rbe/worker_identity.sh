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

# Observe this test's guest without changing its arguments or environment.
observe_process=false
if [[ ${1:-} == --observe-process ]]; then
  observe_process=true
  shift
fi
out="${TEST_UNDECLARED_OUTPUTS_DIR:?}/worker-identity"
mkdir -p "$out" || exit 1
{
  uname -a
  cat /proc/version
  id
  if [[ -r /sys/kernel/notes ]]; then
    sha256sum /sys/kernel/notes
  fi
  for device in /dev/kvm /dev/vhost-net; do
    stat --format='%n type=%F mode=%a uid=%u gid=%g device=%t:%T' "$device"
    printf 'stat_exit=%d\n' "$?"
  done
} > "$out/guest.txt" 2>&1
cat "$out/guest.txt"
if [[ $observe_process == false ]]; then
  exec "$@"
fi

# Optional diagnosis of a signaled test. Read only this disposable guest's
# actual test subtree; never dump process environments or credential files.
set -euo pipefail
(( $# > 0 ))

# Invoked by the exported sampler below.
# shellcheck disable=SC2329
snapshot_process() {
  local pid=$1 destination=$2 file status children child children_file
  if [[ ! $pid =~ ^[0-9]+$ || -n ${seen[$pid]:-} || ! -d /proc/$pid ]]; then
    return
  fi
  seen[$pid]=1
  mkdir -p "$destination/$pid"
  status=0
  timeout --foreground 2s readlink "/proc/$pid/exe" > "$destination/$pid/exe" 2> "$destination/$pid/exe.error" || status=$?
  printf '%s\n' "$status" > "$destination/$pid/exe.status"
  for file in status maps wchan syscall io; do
    status=0
    timeout --foreground 2s cat "/proc/$pid/$file" > "$destination/$pid/$file" 2> "$destination/$pid/$file.error" || status=$?
    printf '%s\n' "$status" > "$destination/$pid/$file.status"
  done
  # A subprocess may be created by any thread, not only the process leader.
  for children_file in "/proc/$pid"/task/*/children; do
    status=0
    children=$(timeout --foreground 2s cat "$children_file" 2>> "$destination/$pid/children.error") || status=$?
    printf '%s %s\n' "$children_file" "$status" >> "$destination/$pid/children.status"
    if (( status != 0 )); then
      continue
    fi
    for child in $children; do
      snapshot_process "$child" "$destination"
    done
  done
}

# Invoked in the bounded child shell.
# shellcheck disable=SC2329
observe_subtree() {
  set -euo pipefail
  local root_pid=$1 directory=$2 sample
  # This loop is additionally bounded by timeout. Actual sample count/times
  # are evidence; tool/read delays do not promise exact five-second offsets.
  for sample in {0..23}; do
    local -A seen=()
    mkdir -p "$directory/$sample"
    date -u --iso-8601=ns > "$directory/$sample/time"
    snapshot_process "$root_pid" "$directory/$sample"
    sleep 5
  done
}

kernel_snapshot() {
  local phase=$1 status=0
  timeout --foreground 5s dmesg > "$out/kernel-$phase.txt" 2> "$out/kernel-$phase.error" || status=$?
  printf '%s\n' "$status" > "$out/kernel-$phase.status"
}

target_pid=
observer_pid=
# shellcheck disable=SC2329
finish_observation() {
  local status=$? observer_status=not-started
  trap - EXIT
  set +e
  # GNU timeout owns a separate process group. Stopping it forwards TERM to
  # only the sampler and its foreground reads, never the tested command.
  if [[ -n $observer_pid ]]; then
    if kill -0 "$observer_pid" 2>/dev/null; then
      kill -TERM "$observer_pid"
    fi
    wait "$observer_pid"
    observer_status=$?
  fi
  printf '%s\n' "$observer_status" > "$out/observer-exit.txt"
  kernel_snapshot after
  printf '%s\n' "$status" > "$out/target-exit.txt"
  # Missing or failed observations do not replace the real test outcome.
  exit "$status"
}
trap finish_observation EXIT

# shellcheck disable=SC2329
forward_and_reap() {
  local signal=$1 status=$2
  trap '' INT TERM
  if [[ -n $target_pid ]]; then
    kill -"$signal" "$target_pid" 2>/dev/null || :
    wait "$target_pid" || :
    target_pid=
  fi
  exit "$status"
}
trap 'forward_and_reap INT 130' INT
trap 'forward_and_reap TERM 143' TERM

kernel_snapshot before
# Once launched, observation errors must not abandon the real child. Its wait
# status below, or the forwarded signal handler, owns the wrapper's outcome.
set +e
"$@" <&0 &
target_pid=$!
printf '%s\n' "$target_pid" > "$out/target-pid.txt"
# The real child has already inherited its environment. Export these only for
# the sampler launched below, leaving the tested command's environment intact.
export -f snapshot_process observe_subtree
# The positional parameters belong to the child shell.
# shellcheck disable=SC2016
timeout --signal=TERM --kill-after=5s 120s bash -c 'observe_subtree "$1" "$2"' _ "$target_pid" "$out/process" &
observer_pid=$!


status=0
wait "$target_pid" || status=$?
target_pid=
exit "$status"
