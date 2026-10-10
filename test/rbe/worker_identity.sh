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

# Observe this test's guest. The optional diagnostic affinity control applies to
# this wrapper and is inherited by its test process and descendants.
pin_first_cpu=false
if [[ ${1:-} == --pin-first-cpu ]]; then
  pin_first_cpu=true
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
cat /proc/cpuinfo > "$out/cpuinfo.txt" || exit 1
for name in current_clocksource available_clocksource; do
  source=/sys/devices/system/clocksource/clocksource0/$name
  if [[ -r $source ]]; then
    cat "$source" > "$out/$name.txt" || exit 1
  else
    printf 'unavailable\n' > "$out/$name.txt"
  fi
done
awk '/^Cpus_allowed/ {print}' /proc/self/status > "$out/affinity-before.txt" || exit 1
if [[ $pin_first_cpu == true ]]; then
  cpu=$(awk '/^Cpus_allowed_list:/ {split($2, cpus, /[-,]/); print cpus[1]}' /proc/self/status)
  [[ $cpu =~ ^[0-9]+$ ]] || exit 1
  taskset -pc "$cpu" "$$" > "$out/taskset.txt" 2>&1 || exit 1
fi
awk '/^Cpus_allowed/ {print}' /proc/self/status > "$out/affinity-after.txt" || exit 1
exec "$@"
