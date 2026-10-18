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
# Buildx lowercases the node name used for its Docker container.
builder=${builder,,}
created=false
layout_created=false
cleanup() {
  local result=$? cleanup_status=0
  set +e
  if [[ ${created} == true ]]; then
    if (( result != 0 )); then
      # Removing the builder also removes the daemon's cancellation reason.
      timeout --kill-after=5s 10s docker inspect --format \
        'running={{.State.Running}} oom_killed={{.State.OOMKilled}} exit_code={{.State.ExitCode}} error={{.State.Error}}' \
        "buildx_buildkit_${builder}0" >&2
      timeout --kill-after=5s 10s docker logs --timestamps --tail 200 \
        "buildx_buildkit_${builder}0" >&2
    fi
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
  if (( result != 0 )) && [[ ${layout_created} == true ]]; then
    rm -rf -- "${layout}" || \
      printf 'Cannot remove partial OCI layout %s\n' "${layout}" >&2
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

"${buildx}" version
"${bsdtar}" --version
# The public digest file comes from the MODULE pin. The stock driver fetches
# the image using Docker's normal verification and image-store mechanisms.
"${buildx}" create --name "${builder}" --driver docker-container \
  --driver-opt "image=${image}"
created=true
# Stock oci_load expects one image manifest, not an attestation index.
# BuildKit retains compressed layers until export completes. Writing directly
# to remotely backed outputs avoids another OCI copy on the root disk.
mkdir -- "${layout}"
layout_created=true
set +e
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
