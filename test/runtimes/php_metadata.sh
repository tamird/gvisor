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

# Validation only: run the existing PHP proctor against released and fixed metadata.
set -euo pipefail
test "$#" -eq 3
proctor="${PWD}/$1"
runtime=$2
mode=$3
out=${TEST_UNDECLARED_OUTPUTS_DIR:?}
image=gvisor.dev/images/runtimes/php8.3.35:latest

docker image inspect "${image}" > "${out}/php-image-inspect.json"
docker info --format '{{json .Runtimes}}' > "${out}/docker-runtimes.json"
if [[ ${mode} != native ]]; then
  grep -F '"runsc":' "${out}/docker-runtimes.json"
  if [[ ${mode} == directfs ]]; then
    grep -F -- '--directfs=true' "${out}/docker-runtimes.json"
  else
    test "${mode}" = goferfs
    grep -F -- '--directfs=false' "${out}/docker-runtimes.json"
  fi
fi

for state in before after; do
  set +e
  docker run --rm --runtime="${runtime}" --volume "${proctor}:/proctor:ro" \
    "${image}" /bin/bash -c '
      set -euo pipefail
      testfile=ext/intl/tests/gh17469.phpt
      sapi/cli/php -v
      sapi/cli/php -r '\''exit(extension_loaded("intl") ? 1 : 0);'\''
      printf "%s  %s\n" 0fafad481097254b10b59eeb0b7cc5043178ee580c4cd13602d423771e89a8c3 \
        "${testfile}" | sha256sum --check -
      if [[ $1 == before ]]; then
        # The checked hash fixes these as the two backported metadata lines.
        sed -i "3,4d" "${testfile}"
        printf "%s  %s\n" aa08b684b72de5b24ec8c6da735f51bf379066ce856c179e14b82a7cf9655a5f \
          "${testfile}" | sha256sum --check -
      fi
      exec /proctor --runtime=php --tests="${testfile}" --timeout=2m
    ' php-metadata "${state}" 2>&1 | tee "${out}/${state}.log"
  results=("${PIPESTATUS[@]}")
  set -e
  printf '%s\n' "${results[0]}" > "${out}/${state}-exit.txt"
  test "${results[1]}" -eq 0
  grep -F 'PHP 8.3.35 ' "${out}/${state}.log"
  if [[ ${state} == before ]]; then
    test "${results[0]}" -eq 1
    grep -F 'Class "UConverter" not found' "${out}/${state}.log"
    grep -F 'FAILED TEST SUMMARY' "${out}/${state}.log"
  else
    test "${results[0]}" -eq 0
    grep -F 'GH-17469:' "${out}/${state}.log" | \
      grep -F '[ext/intl/tests/gh17469.phpt] reason: Required extension missing: intl'
  fi
  printf 'PHP_METADATA %s %s exit=%s\n' "${mode}" "${state}" "${results[0]}"
done
