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

make="${TEST_SRCDIR:?}/@@TOOLS@@/@@MAKE@@"
crane="${TEST_SRCDIR}/@@TOOLS@@/crane"
makefile="${PWD}/@@MAKEFILE@@"
contexts="${PWD}/@@CONTEXTS@@"
work=$(mktemp -d "${TEST_TMPDIR:?}/image-sources.XXXXXX")
trap 'rm -rf "${work}"' EXIT

# The declared archive carries source modes that Bazel runfiles do not retain.
tar --extract --file "${contexts}" --directory "${work}" \
  --same-permissions --no-same-owner

# Bazel shards are zero-based; the canonical Make partitions are one-based.
if [[ -n ${TEST_SHARD_STATUS_FILE:-} ]]; then
  touch "${TEST_SHARD_STATUS_FILE}"
fi
partition=$((${TEST_SHARD_INDEX:-0} + 1))
partitions=${TEST_TOTAL_SHARDS:-1}

# CRANE is a shell command in images.mk, so quote the declared executable.
printf -v crane_command '%q' "${crane}"
export MAKE="${make}"
cd "${work}"
"${make}" -f "${makefile}" \
  ARCH=@@ARCH@@ PARTITION="${partition}" TOTAL_PARTITIONS="${partitions}" \
  CRANE="${crane_command}" test-@@IMAGE_CLASS@@-images
