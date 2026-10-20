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
  # This qualification branch exercises the public Node recipe once with the
  # legacy Docker builder. The canonical Makefile supplies these two values.
  test "${arch}" = x86_64
  test "${selection}" = cpu
  test "${TEST_SHARD_INDEX:-0}" -eq 0
  test "${TEST_TOTAL_SHARDS:-1}" -eq 1
  export DOCKER_BUILDKIT=0
  printf 'Node plain Docker build: DOCKER_BUILDKIT=%s\n' "${DOCKER_BUILDKIT}"
  sha256sum "${makefile}" images/runtimes/nodejs26.12.0/*
  "${make}" -f "${makefile}" ARCH="${arch}" SHELL=/bin/bash \
    'header=echo --- $(1) >&2' rebuild-runtimes_nodejs26.12.0
  image=$("${make}" --no-print-directory -s -f "${makefile}" \
    ARCH="${arch}" SHELL=/bin/bash 'header=echo --- $(1) >&2' \
    local-image-runtimes_nodejs26.12.0)
  docker image inspect --format '{{json .}}' "${image}"
  image_id=$(docker image inspect --format '{{.Id}}' "${image}")
  version=$(docker run --rm "${image}" /root/node-v26.12.0/node --version)
  test "${version}" = v26.12.0
  printf 'Node plain Docker image: %s %s %s\n' "${image}" "${image_id}" "${version}"
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

  # Source archives use the canonical rebuild target and pinned BuildKit image.
  # A published image with the same context hash is not source-build evidence.
  printf -v build_command '/bin/bash %q %q %q %q %q' \
    "${builder}" "${buildx}" "${buildkit_digest}" "${bsdtar}" "${layout}"
  # Some executors create empty output directories before invoking the action.
  # Require that any precreated directory is empty before taking ownership.
  if [[ -e ${layout} ]]; then
    rmdir "${layout}"
  fi
  "${make}" -f "${makefile}" ARCH="${arch}" _IMAGE_CONTEXT_PREPARED=1 \
    OCI_BUILD="${build_command}" \
    _IMAGE_OCI_OUTPUT="${layout}" _IMAGE_OCI_PLATFORM="${platform}" \
    "rebuild-${target}"

  # The builder produces the declared layout before this action succeeds.
  # Bazel then exposes it to downstream conversion.
  printf '%s\n' "${references[1]}" "${references[2]}" > "${tags}"
  printf 'Source OCI tags: %s %s\n' "${references[1]}" "${references[2]}"
fi
