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
test "$#" -eq 8
arch=$1
make=$(realpath "$2")
crane=$(realpath "$3")
makefile=$(realpath "$4")
contexts=$(realpath "$5")
selection=$6
layout=$(realpath -m "$7")
tags=$(realpath -m "$8")
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
# Fork-only bounded worker comparison. This stops before declared layout
# publication and any stock archive conversion, fallback or Docker build.
saved="${work}/layout"
printf 'Declared output publication skipped: %s %s\n' "${layout}" "${tags}"
printf 'Worker probe: %s\n' "$(date --iso-8601=ns)"
uname -a
head -30 /proc/cpuinfo
sha256sum "${crane}"
od -An -tx1 -N64 "${crane}"
"${crane}" version
for file in /proc/sys/net/ipv4/tcp_rmem /proc/sys/net/ipv4/tcp_wmem \
  /sys/fs/cgroup/cpu.max /sys/fs/cgroup/memory.max /proc/net/dev; do
  printf 'BEFORE %s\n' "${file}"
  if [[ -r ${file} ]]; then cat "${file}"; else printf 'unavailable\n'; fi
done
df -B1 "${work}"
set +e
time timeout --signal=QUIT --kill-after=5s 120s \
  env GODEBUG=http2client=0 "${crane}" pull --format=oci \
  --platform="${platform}" "${references[0]}" "${saved}"
transfer_status=$?
set -e
printf 'Transfer status: %s at %s\n' "${transfer_status}" "$(date --iso-8601=ns)"
printf 'AFTER /proc/net/dev\n'
cat /proc/net/dev
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
# Even a completed pull is diagnostic only: never copy or publish a large tree.
if (( transfer_status == 0 )); then
  printf 'Pull completed within the probe; output publication intentionally skipped.\n'
  exit 3
fi
exit "${transfer_status}"
