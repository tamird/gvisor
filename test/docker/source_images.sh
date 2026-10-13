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
  layout)
    test "$#" -eq 13
    layout=$(realpath -m "$8")
    tags=$(realpath -m "$9")
    buildx=$(realpath "${10}")
    buildkit_digest=$(realpath "${11}")
    builder=$(realpath "${12}")
    bsdtar=$(realpath "${13}")
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
  case "${arch}" in
    x86_64) platform=linux/amd64 ;;
    aarch64) platform=linux/arm64 ;;
    *) echo "Unknown image architecture: ${arch}" >&2; exit 1 ;;
  esac
  "${make}" --no-print-directory -s -f "${makefile}" ARCH="${arch}" \
    _IMAGE_CONTEXT_PREPARED=1 "image-references-${target}" > references
  mapfile -t references < references
  test "${#references[@]}" -eq 3

  # Keep the transport used for the qualified concurrent OCI exports local to
  # the cache read. A miss uses the declared Buildx client and BuildKit image.
  printf -v crane_command 'env GODEBUG=http2client=0 %q' "${crane}"
  printf -v build_command '/bin/bash %q %q %q %q %q' \
    "${builder}" "${buildx}" "${buildkit_digest}" "${bsdtar}" "${layout}"
  # Some executors create empty output directories before invoking the action.
  # Require that any precreated directory is empty before taking ownership.
  if [[ -e ${layout} ]]; then
    rmdir "${layout}"
  fi
  "${make}" -f "${makefile}" ARCH="${arch}" _IMAGE_CONTEXT_PREPARED=1 \
    CRANE="${crane_command}" OCI_BUILD="${build_command}" \
    _IMAGE_OCI_OUTPUT="${layout}" _IMAGE_OCI_PLATFORM="${platform}" \
    "export-oci-${target}"

  # Both standard exporters write the declared layout directly. Bazel only
  # exposes it to downstream conversion after this action succeeds.
  printf '%s\n' "${references[1]}" "${references[2]}" > "${tags}"
  printf 'Source OCI tags: %s %s\n' "${references[1]}" "${references[2]}"
fi
