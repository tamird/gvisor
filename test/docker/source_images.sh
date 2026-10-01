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

# Validation branch only: build the source image before the existing PHP runner.
set -euo pipefail
test "$#" -eq 5
runfiles=${PWD}
runner="${runfiles}/$1"
proctor="${PWD}/$2"
exclude="${PWD}/$3"
partition=$4
partitions=$5
make="${TEST_SRCDIR:?}/@@TOOLS@@/@@MAKE@@"
crane="${TEST_SRCDIR}/@@TOOLS@@/crane"
makefile="${PWD}/@@MAKEFILE@@"
contexts="${PWD}/@@CONTEXTS@@"
out=${TEST_UNDECLARED_OUTPUTS_DIR:?}
work=$(mktemp -d "${TEST_TMPDIR:?}/php-source.XXXXXX")
trap 'rm -rf "${work}"' EXIT
if [[ -n ${TEST_SHARD_STATUS_FILE:-} ]]; then
  touch "${TEST_SHARD_STATUS_FILE}"
fi
test "${TEST_TOTAL_SHARDS:-1}" -eq 1

# The shared daemon registers the declared runsc, but builds images with runc.
test "$(docker info --format '{{.DefaultRuntime}}')" = runc
docker info --format '{{json .Runtimes}}' > "${out}/docker-runtimes.json"
grep -F '"runsc":' "${out}/docker-runtimes.json"
tar --extract --file "${contexts}" --directory "${work}" \
  --same-permissions --no-same-owner
printf -v crane_command '%q' "${crane}"
export MAKE="${make}"
cd "${work}"
"${make}" -f "${makefile}" ARCH=@@ARCH@@ CRANE="${crane_command}" \
  load-runtimes_php8.3.35 2>&1 | tee "${out}/php-image-build.log"
image=$("${make}" --no-print-directory -s -f "${makefile}" \
  ARCH=@@ARCH@@ CRANE="${crane_command}" local-image-runtimes_php8.3.35)
printf '%s\n' "${image}" > "${out}/php-image.txt"
docker image inspect "${image}" > "${out}/php-image-inspect.json"
test "$(docker image inspect --format '{{.Id}}' "${image}")" = \
  "$(docker image inspect --format '{{.Id}}' gvisor.dev/images/runtimes/php8.3.35:latest)"
docker run --rm --runtime=runc "${image}" sapi/cli/php -v | tee "${out}/php-version.txt"
grep -F 'PHP 8.3.35 ' "${out}/php-version.txt"
docker run --rm --runtime=runc "${image}" cat Zend/tests/concat_003.phpt > "${out}/concat_003.phpt"
printf '%s  %s\n' be2ac2b32e73c3c7030c08f777d5878306b0cbfa3b3e56f22bfd64ea8af24c8a \
  "${out}/concat_003.phpt" | sha256sum --check -
docker run --rm --runtime=runc --volume "${proctor}:/proctor:ro" \
  "${image}" /proctor --runtime=php --list > "${out}/php-test-inventory.txt"
test -s "${out}/php-test-inventory.txt"

# Preserve the existing batching, exclusions, partitioning, retries and errors.
# Explicit selection uses registered runsc, independently of the build default.
cd "${runfiles}"
set +e
"${runner}" --runtime=runsc --lang=php --image=php8.3.35 --batch=50 --timeout=40m \
  "--exclude_file=${exclude}" "--partition=${partition}" \
  "--total_partitions=${partitions}" 2>&1 | tee "${out}/php-runner.log"
results=("${PIPESTATUS[@]}")
set -e
printf '%d\n' "${results[0]}" > "${out}/php-runner-exit.txt"
printf '%d\n' "${results[1]}" > "${out}/php-capture-exit.txt"
test "${results[0]}" -eq 0
test "${results[1]}" -eq 0
