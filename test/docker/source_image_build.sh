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
created=false
cleanup() {
  local result=$? cleanup_status=0
  set +e
  if [[ ${created} == true ]]; then
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

# A large export received SIGBUS while this client ran from BuildBuddy's VFS.
# Keep a scratch copy until direct execution can be validated on that worker.
cp -- "${buildx}" "${work}/buildx"
chmod 0555 "${work}/buildx"
buildx="${work}/buildx"
staged_layout="${work}/oci-layout"

"${buildx}" version
"${bsdtar}" --version
# The public digest file comes from the MODULE pin. The stock driver fetches
# the image using Docker's normal verification and image-store mechanisms.
"${buildx}" create --name "${builder}" --driver docker-container \
  --driver-opt "image=${image}"
created=true
# Stock oci_load expects one image manifest, not an attestation index.
# BuildKit's health checks stay active during export. Finish that session before
# copying the layout to remotely served outputs.
mkdir -- "${layout}" "${staged_layout}"
set +e
"${buildx}" build --builder "${builder}" --provenance=false --progress=plain \
  --output=type=oci,tar=true,compression=gzip,force-compression=true,dest=- "$@" | \
  "${bsdtar}" --extract -b 2048 --file - --directory "${staged_layout}" --no-same-owner
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
cp -R -- "${staged_layout}/." "${layout}/"
