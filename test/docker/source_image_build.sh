#!/bin/bash

# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -euo pipefail
test "$#" -ge 5
buildx=$1
image_digest=$2
bsdtar=$3
layout=$4
shift 4
image="docker.io/moby/buildkit@$(cat "${image_digest}")"

work=$(mktemp -d "${TMPDIR:-/tmp}/source-image-builder.XXXXXX")
export BUILDX_CONFIG="${work}"
builder="gvisor-source-${work##*.}"
# Buildx lowercases names before assigning its default node. Use that same
# spelling for creation and the exact owned-container observations below.
builder=${builder,,}
container="buildx_buildkit_${builder}0"
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
created=false
# Qualification-only observations are bounded and never replace the build's
# status. Restrict Docker reads to this helper's private builder container.
observe() {
  local label=$1 status=0
  shift
  printf 'Source stream observation %s at %s\n' "${label}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
  timeout --signal=TERM --kill-after=1s 5s "$@" >&2 || status=$?
  printf 'Source stream observation %s status=%s\n' "${label}" "${status}" >&2
}
observe_guest() {
  observe memory cat /proc/meminfo /proc/pressure/memory
  observe filesystems df -T -- "${work}" "${layout}"
}
cleanup() {
  local result=$? cleanup_status=0
  set +e
  observe_guest
  observe kernel /bin/bash -o pipefail -c 'dmesg --color=never | tail -n 160'
  if [[ ${created} == true ]]; then
    observe builder-state docker inspect --format \
      'id={{.Id}} name={{.Name}} image={{.Image}} state={{json .State}} restarts={{.RestartCount}} memory_limit={{.HostConfig.Memory}} swap_limit={{.HostConfig.MemorySwap}}' \
      "${container}"
    observe builder-stats docker stats --no-stream --format \
      '{{.ID}} memory={{.MemUsage}} memory_percent={{.MemPerc}} pids={{.PIDs}}' "${container}"
    # The observed child shell expands its own positional argument.
    # shellcheck disable=SC2016
    observe builder-log /bin/bash -o pipefail -c \
      'docker logs --timestamps --tail 200 "$1" 2>&1 | head -c 262144' source-stream-log "${container}"
    observe builder-events docker events --since "${started_at}" \
      --until "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --filter "container=${container}" \
      --format '{{.TimeNano}} {{.Action}} {{.Actor.ID}} exit={{index .Actor.Attributes "exitCode"}}'
    "${buildx}" rm --force "${builder}"
    cleanup_status=$?
  fi
  if (( cleanup_status == 0 )); then
    rm -rf "${work}"
    cleanup_status=$?
  else
    printf 'Failed to remove builder %s; state directory remains at %s.\n' \
      "${builder}" "${work}" >&2
  fi
  if (( result == 0 && cleanup_status != 0 )); then
    result=${cleanup_status}
  fi
  exit "${result}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Qualification-only: retain the copied executable through this helper's
# builder removal. cleanup removes its private directory only after Buildx rm.
work=$(realpath "${work}")
printf 'Buildx staging source: %s\nBuildx staging directory: %s\n' "${buildx}" "${work}"
df -T -- "${buildx}" "${work}"
if [[ ${buildx} != /vfs/* || ${work} != /tmp/* ]] || \
   [[ $(stat -f -c %t "${buildx}") == $(stat -f -c %t "${work}") ]]; then
  echo 'Expected VFS input and a separate /tmp filesystem for this diagnostic' >&2
  exit 1
fi
source_digest=$(sha256sum -- "${buildx}")
staged_buildx=$(mktemp "${work}/buildx.XXXXXX")
cp -- "${buildx}" "${staged_buildx}"
chmod 0555 "${staged_buildx}"
staged_digest=$(sha256sum -- "${staged_buildx}")
printf 'Buildx source SHA256: %s\nBuildx staged SHA256: %s\n' \
  "${source_digest%% *}" "${staged_digest%% *}"
test "${source_digest%% *}" = "${staged_digest%% *}"
buildx=${staged_buildx}

"${buildx}" version
"${bsdtar}" --version
# The public digest file comes from the MODULE pin. The stock driver fetches
# the image using Docker's normal verification and image-store mechanisms.
"${buildx}" create --name "${builder}" --driver docker-container \
  --driver-opt "image=${image}"
created=true
# Stock oci_load expects one image manifest, not an attestation index.
# Stream the standard OCI archive into its layout without retaining a second
# full archive or using Buildx's client content-store output path.
mkdir -- "${layout}"
observe_guest
set +e
# Qualification-only: compare the stock 1 MiB pipe-read request with the
# default 10 KiB buffer, keeping the output filesystem and pipeline unchanged.
"${buildx}" build --builder "${builder}" --provenance=false --progress=plain \
  --output=type=oci,tar=true,compression=gzip,force-compression=true,dest=- "$@" | \
  "${bsdtar}" --extract -b 2048 --file - --directory "${layout}" --no-same-owner
statuses=("${PIPESTATUS[@]}")
set -e
printf 'Source OCI export status: buildx=%s tar=%s\n' "${statuses[0]}" "${statuses[1]}" >&2
if (( statuses[0] != 0 )); then
  exit "${statuses[0]}"
fi
if (( statuses[1] != 0 )); then
  exit "${statuses[1]}"
fi
printf 'Source builder image %s: ' "${image}"
docker image inspect --format '{{.Id}}' "${image}"
