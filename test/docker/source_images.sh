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
    test "$#" -eq 8
    archive=$(realpath -m "$8")
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
  "${make}" -f "${makefile}" ARCH="${arch}" \
    PARTITION="${partition}" TOTAL_PARTITIONS="${partitions}" \
    CRANE="${crane_command}" "test-${selection}-images"
else
  target=${selection//\//_}
  "${make}" -f "${makefile}" ARCH="${arch}" \
    CRANE="${crane_command}" "load-${target}"
  image=$("${make}" --no-print-directory -s -f "${makefile}" \
    ARCH="${arch}" CRANE="${crane_command}" "local-image-${target}")
  latest="gvisor.dev/images/${selection}:latest"
  id=$(docker image inspect --format '{{.Id}}' "${image}")
  test "${id}" = "$(docker image inspect --format '{{.Id}}' "${latest}")"
  printf 'Source image %s (%s); consumer tag %s\n' "${image}" "${id}" "${latest}"
  # Consumers use the canonical latest name in their fresh private daemons.
  docker save --output "${archive}" "${image}" "${latest}"
  sha256sum "${archive}"
fi
