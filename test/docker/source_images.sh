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

mode=$1
arch=$2
# Resolve action/runfiles paths before entering the extracted source tree.
make=$(realpath "$3")
crane=$(realpath "$4")
makefile=$(realpath "$5")
contexts=$(realpath "$6")
selection=$7
case "${mode}" in
  test)
    test "$#" -eq 7
    if [[ -n ${TEST_SHARD_STATUS_FILE:-} ]]; then
      touch "${TEST_SHARD_STATUS_FILE}"
    fi
    ;;
  archive)
    test "$#" -eq 10
    archive=$(realpath -m "$8")
    retag=$(realpath "$9")
    network_tools=$(realpath "${10}")
    ;;
  *) echo "Unknown image source operation: ${mode}" >&2; exit 1 ;;
esac
work=$(mktemp -d "${TEST_TMPDIR:-${TMPDIR:-/tmp}}/image-sources.XXXXXX")
trap 'rm -rf "${work}"' EXIT

# The declared archive carries source modes that Bazel runfiles do not retain.
tar --extract --file "${contexts}" --directory "${work}" \
  --same-permissions --no-same-owner

# CRANE is a shell command in images.mk, so quote the declared executable.
printf -v crane_command '%q' "${crane}"
export MAKE="${make}"
cd "${work}"
if [[ ${mode} == test ]]; then
  # Bazel shards are zero-based; canonical Make partitions are one-based.
  partition=$((${TEST_SHARD_INDEX:-0} + 1))
  partitions=${TEST_TOTAL_SHARDS:-1}
  "${make}" -f "${makefile}" ARCH="${arch}" _IMAGE_CONTEXT_PREPARED=1 \
    PARTITION="${partition}" TOTAL_PARTITIONS="${partitions}" \
    CRANE="${crane_command}" "test-${selection}-images"
else
  target=${selection//\//_}
  "${make}" --no-print-directory -s -f "${makefile}" ARCH="${arch}" \
    _IMAGE_CONTEXT_PREPARED=1 CRANE="${crane_command}" \
    "image-references-${target}" > "${work}/references"
  mapfile -t references < "${work}/references"
  test "${#references[@]}" -eq 3
  remote=${references[0]}
  image=${references[1]}
  latest=${references[2]}
  case "${arch}" in
    x86_64) platform=linux/amd64 ;;
    aarch64) platform=linux/arm64 ;;
    *) echo "Unknown image architecture: ${arch}" >&2; exit 1 ;;
  esac
  # Fork-only comparison uses crane's concurrent compressed-layer OCI writer.
  # Partial blobs are diagnostic data, not a verified image or Docker archive.
  saved="${work}/image.oci"
  # Fork-only diagnostic: capture the stalled command before retrying a full export.
  printf 'Transfer diagnostic: remote=%s platform=%s spool=%s\n' "${remote}" "${platform}" "${saved}"
  uname -m
  od -An -tx1 -N64 "${crane}"
  head -30 /proc/cpuinfo
  "${crane}" version
  # Reuse the complete pinned KVM userspace archive as private read-only tools.
  # It includes iproute2 and its loader/library closure; nothing is installed.
  network_root="${work}/network-tools"
  mkdir "${network_root}"
  tar --extract --file "${network_tools}" --directory "${network_root}" \
    --same-permissions --no-same-owner
  network_log="${work}/network.log"
  observer_pid=
  # The observer is one bounded process group, reaped before its log is removed.
  cleanup_network() {
    local status=$? observer_status=0
    trap - EXIT
    set +e
    if [[ -n ${observer_pid} ]]; then
      kill -TERM "${observer_pid}" 2>/dev/null
      wait "${observer_pid}"
      observer_status=$?
    fi
    printf 'Network observer status: %s\n' "${observer_status}"
    cat "${network_log}"
    rm -rf "${work}"
    exit "${status}"
  }
  trap cleanup_network EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT
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
    "${crane}" pull --format=oci --platform="${platform}" "${remote}" "${saved}"
  transfer_status=$?
  set -e
  printf 'Transfer status: %s\n' "${transfer_status}"
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
    df -B1 "${saved}"
  fi
  # Never fall through into a second pull or a full Docker build after profiling.
  exit "${transfer_status}"
  if [[ ${SKIP_IMAGE_LOAD:-} != true ]] && \
      "${crane}" pull --platform="${platform}" "${remote}" "${saved}"; then
    "${retag}" "${saved}" "${remote}" "${image}" "${latest}"
  else
    # Retain the canonical pull/rebuild and local-only policies when the
    # crane command fails, including a registry-read or spool-write error.
    # Later retag and publication errors do not enter this fallback.
    rm -f "${saved}"
    "${make}" -f "${makefile}" ARCH="${arch}" _IMAGE_CONTEXT_PREPARED=1 \
      CRANE="${crane_command}" "load-${target}"
    id=$(docker image inspect --format '{{.Id}}' "${image}")
    test "${id}" = "$(docker image inspect --format '{{.Id}}' "${latest}")"
    printf 'Source image %s (%s); consumer tag %s\n' "${image}" "${id}" "${latest}"
    docker save --output "${saved}" "${image}" "${latest}"
  fi
  cp "${saved}" "${archive}"
  sha256sum "${archive}"
fi
