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
test "$#" -eq 9
arch=$1
make=$(realpath "$2")
crane=$(realpath "$3")
makefile=$(realpath "$4")
contexts=$(realpath "$5")
selection=$6
layout=$(realpath -m "$7")
tags=$(realpath -m "$8")
network_tools=$(realpath "$9")
work=$(mktemp -d "${TMPDIR:-/tmp}/cached-source-oci.XXXXXX")
trap 'rm -rf "${work}"' EXIT

tar --extract --file "${contexts}" --directory "${work}" \
  --same-permissions --no-same-owner
printf -v crane_command '%q' "${crane}"
export MAKE="${make}"
cd "${work}"
target=${selection//\//_}
"${make}" --no-print-directory -s -f "${makefile}" ARCH="${arch}" \
  _IMAGE_CONTEXT_PREPARED=1 CRANE="${crane_command}" \
  "image-references-${target}" > references
mapfile -t references < references
test "${#references[@]}" -eq 3
case "${arch}" in
  x86_64) platform=linux/amd64 ;;
  aarch64) platform=linux/arm64 ;;
  *) echo "Unknown image architecture: ${arch}" >&2; exit 1 ;;
esac
printf 'Cached OCI source: %s platform=%s\n' "${references[0]}" "${platform}"
# Fork-only two-arm probe in a disposable Firecracker VM. These guards run
# before any guest setting is changed; no ordinary executor or host is eligible.
printf 'Receive probe worker: %s\n' "$(date --iso-8601=ns)"
uname -a
head -30 /proc/cpuinfo
sha256sum "${crane}"
"${crane}" version
printf 'Declared publication skipped: %s %s\n' "${layout}" "${tags}"
test "$(id -u)" -eq 0
test "$(uname -r)" = 6.1.0
boot_args=$(cat /proc/cmdline)
# BuildBuddy's pinned getBootArgs sets these for its networked VFS guest.
# https://github.com/buildbuddy-io/buildbuddy/blob/5608c55ae/enterprise/server/remote_execution/containers/firecracker/firecracker.go#L1746-L1792
[[ " ${boot_args} " == *" -set_default_route "* ]]
[[ " ${boot_args} " == *" -enable_vfs "* ]]
printf 'Guest boot preconditions: networked VFS kernel6.1.0\n'
readonly receive_setting=/proc/sys/net/ipv4/tcp_rmem
original_receive=$(cat "${receive_setting}")
read -r receive_min receive_default receive_max <<< "${original_receive}"
test "${receive_min} ${receive_default} ${receive_max}" = '4096 131072 6291456'
printf 'Original tcp_rmem: %s\n' "${original_receive}"
network_root="${work}/network-tools"
mkdir "${network_root}"
tar --extract --file "${network_tools}" --directory "${network_root}" \
  --same-permissions --no-same-owner
saved="${work}/layout"
observer_pid=
network_log=
receive_modified=0
# shellcheck disable=SC2329
cleanup_probe() {
  local status=$? restore_status=0 observer_status=0 cleanup_status=0
  trap - EXIT
  set +e
  if [[ -n ${observer_pid} ]]; then
    kill -TERM "${observer_pid}" 2>/dev/null
    wait "${observer_pid}"
    observer_status=$?
    printf 'Interrupted network observer status: %s\n' "${observer_status}"
    cat "${network_log}"
  fi
  if (( receive_modified )); then
    printf '%s\n' "${original_receive}" > "${receive_setting}"
    restore_status=$?
    restored_receive=$(cat "${receive_setting}")
    [[ ${restored_receive} == "${original_receive}" ]] || restore_status=1
    printf 'Restored tcp_rmem: %s\nRestore status: %s\n' "${restored_receive}" "${restore_status}"
  fi
  rm -rf "${work}"
  cleanup_status=$?
  printf 'Work cleanup status: %s\n' "${cleanup_status}"
  if (( restore_status != 0 || cleanup_status != 0 )); then exit 1; fi
  exit "${status}"
}
trap cleanup_probe EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
run_probe() {
  local arm=$1 transfer_status=0 observer_status=0
  printf 'BEGIN RECEIVE ARM %s %s\n' "${arm}" "$(date --iso-8601=ns)"
  printf 'Arm tcp_rmem: %s\n' "$(cat "${receive_setting}")"
  df -B1 "${work}"
  network_log="${work}/${arm}-network.log"
  # At most four timestamped snapshots within105s; no fixed-offset guarantee.
  timeout --signal=TERM --kill-after=3s 105s /bin/bash -s -- "${network_root}" > "${network_log}" 2>&1 <<'NETWORK' &
set +e
printf 'BEGIN NETWORK OBSERVATIONS\n'
uname -a
for key in \
  net/core/rmem_default net/core/rmem_max net/core/wmem_default \
  net/core/wmem_max net/core/netdev_max_backlog net/ipv4/tcp_rmem \
  net/ipv4/tcp_wmem net/ipv4/tcp_mem net/ipv4/tcp_moderate_rcvbuf \
  net/ipv4/tcp_window_scaling net/ipv4/tcp_congestion_control \
  net/ipv4/tcp_mtu_probing; do
  if [[ -r /proc/sys/${key} ]]; then
    printf '%s=' "${key}"
    cat "/proc/sys/${key}"
  else
    printf '%s unavailable\n' "${key}"
  fi
done
network_root=$1
loader="${network_root}/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
libraries="${network_root}/lib/x86_64-linux-gnu:${network_root}/usr/lib/x86_64-linux-gnu"
if [[ ! -x ${loader} ]]; then
  printf 'Declared loader unavailable: %s\n' "${loader}"
  exit 1
fi
declare -A tools
for tool in ip tc ss; do
  for directory in usr/bin usr/sbin bin sbin; do
    candidate="${network_root}/${directory}/${tool}"
    if [[ -x ${candidate} ]]; then tools[${tool}]=${candidate}; break; fi
  done
  if [[ -z ${tools[${tool}]:-} ]]; then
    printf 'Declared tool unavailable: %s\n' "${tool}"
    exit 1
  fi
  printf 'Declared %s: %s\n' "${tool}" "${tools[${tool}]}"
  "${loader}" --library-path "${libraries}" --list "${tools[${tool}]}"
  printf '%s loader-list status: %s\n' "${tool}" "$?"
done
"${loader}" --library-path "${libraries}" "${tools[ss]}" --version
timeout --foreground 3s "${loader}" --library-path "${libraries}" "${tools[ip]}" -s link
printf 'ip-link status: %s\n' "$?"
timeout --foreground 3s "${loader}" --library-path "${libraries}" "${tools[ip]}" route
printf 'ip-route status: %s\n' "$?"
timeout --foreground 3s "${loader}" --library-path "${libraries}" "${tools[tc]}" -s qdisc show
printf 'tc status: %s\n' "$?"
for sample in 0 1 2 3; do
  printf 'NETWORK SAMPLE %s %s\n' "${sample}" "$(date --iso-8601=ns)"
  timeout --foreground --signal=TERM --kill-after=1s 3s \
    "${loader}" --library-path "${libraries}" "${tools[ss]}" -tinm
  printf 'ss status: %s\n' "$?"
  for file in /proc/net/sockstat /proc/net/netstat /proc/net/snmp; do
    printf '%s\n' "${file}"
    cat "${file}"
  done
  if (( sample < 3 )); then sleep 30; fi
done
printf 'END NETWORK OBSERVATIONS\n'
NETWORK
  observer_pid=$!
  set +e
  time timeout --signal=QUIT --kill-after=5s 120s \
    env GODEBUG=http2client=0 "${crane}" pull --format=oci \
    --platform="${platform}" "${references[0]}" "${saved}"
  transfer_status=$?
  set -e
  printf 'Arm transfer status: %s\n' "${transfer_status}"
  wait "${observer_pid}" || observer_status=$?
  observer_pid=
  printf 'Arm network observer status: %s\n' "${observer_status}"
  cat "${network_log}"
  if [[ -d ${saved} ]]; then
    du --bytes --summarize "${saved}"
    for blob in "${saved}"/blobs/*/*; do
      if [[ -f ${blob} ]]; then
        stat --format='OCI blob=%n size=%s blocks=%b block-size=%B' "${blob}"
      fi
    done
    for metadata in oci-layout index.json; do
      if [[ -f ${saved}/${metadata} ]]; then
        printf 'OCI metadata %s\n' "${metadata}"
        cat "${saved}/${metadata}"
      fi
    done
  fi
  df -B1 "${work}"
  printf 'END RECEIVE ARM %s %s\n' "${arm}" "$(date --iso-8601=ns)"
  # A different early failure cannot provide the planned paired comparison.
  test "${transfer_status}" -eq 124
}
run_probe default
# The next crane process must not reuse the first arm's partial files.
rm -rf "${saved}"
receive_modified=1
printf '%s\n' '8192 262144 536870912' > "${receive_setting}"
read -r receive_min receive_default receive_max < "${receive_setting}"
test "${receive_min} ${receive_default} ${receive_max}" = '8192 262144 536870912'
run_probe ordinary_receive_tuple
# Deliberately stop before layout publication, archive conversion or fallback.
exit 124
