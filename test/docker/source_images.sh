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
    test "$#" -eq 9
    archive=$(realpath -m "$8")
    retag=$(realpath "$9")
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
  # Retain the local-filesystem spool while avoiding a Docker unpack/save for
  # cached source images. The retagger changes
  # only crane's final manifest, so no second full archive is needed.
  saved="${work}/image.tar"
  # Fork-only diagnostic: capture the stalled command before retrying a full export.
  printf 'Transfer diagnostic: remote=%s platform=%s spool=%s\n' "${remote}" "${platform}" "${saved}"
  uname -m
  od -An -tx1 -N64 "${crane}"
  head -30 /proc/cpuinfo
  "${crane}" version
  set +e
  time timeout --signal=QUIT --kill-after=5s 120s \
    "${crane}" pull --platform="${platform}" "${remote}" "${saved}"
  transfer_status=$?
  set -e
  printf 'Transfer status: %s\n' "${transfer_status}"
  if [[ -f ${saved} ]]; then
    stat --format='Spool size=%s blocks=%b block-size=%B' "${saved}"
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
