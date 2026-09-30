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
work=$(mktemp -d "${TEST_TMPDIR:?}/image-sources.XXXXXX")
trap 'rm -rf "${work}"' EXIT

# Contexts currently contain only regular files (Git modes 0644 and 0755).
# Dereference Bazel's runfiles links so find -type f sees the same inputs;
# restore the owner-write bit removed by read-only input materialization.
# This does not preserve a future tracked source symlink as cp -a would.
cp -RLp images "${work}/images"
chmod -R u+w "${work}/images"

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
  CRANE="${crane_command}" test-cpu-images
