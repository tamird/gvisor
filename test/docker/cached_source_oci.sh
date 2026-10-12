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
# Qualification of the observed parallel HTTP1 path only. A cache miss fails;
# the production Make pull/rebuild owner remains untouched by this prototype.
env GODEBUG=http2client=0 "${crane}" pull --format=oci \
  --platform="${platform}" "${references[0]}" "${work}/layout"
# Preserve the proven ext4 staging boundary before publishing declared inputs.
mkdir -p "${layout}"
cp -a "${work}/layout/." "${layout}/"
printf '%s\n' "${references[1]}" "${references[2]}" > "${tags}"
printf 'Cached OCI tags: %s %s\n' "${references[1]}" "${references[2]}"
du --bytes --summarize "${work}/layout" "${layout}"
df -B1 "${work}/layout"
