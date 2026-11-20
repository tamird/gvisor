#!/usr/bin/env bash

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
helper="$(realpath "$1")"
scratch="$(mktemp -d "$TEST_TMPDIR/sudo-test.XXXXXX")"
trap 'rm -rf "$scratch"' EXIT

for result in 0 7 143; do
  report="$scratch/$result.xml"
  outputs="$scratch/$result.outputs"
  outside="$scratch/$result.outside"
  set +e
  XML_OUTPUT_FILE="$report" TEST_UNDECLARED_OUTPUTS_DIR="$outputs" \
    "$helper" bash -c '
      umask 077
      mkdir -p "$TEST_UNDECLARED_OUTPUTS_DIR/private"
      printf "report" > "$XML_OUTPUT_FILE"
      touch "$TEST_UNDECLARED_OUTPUTS_DIR/private/log" "$2"
      ln -s "$2" "$TEST_UNDECLARED_OUTPUTS_DIR/link"
      if [[ "$1" == 143 ]]; then kill -TERM $$; fi
      exit "$1"
    ' _ "$result" "$outside"
  actual=$?
  set -e
  [[ "$actual" == "$result" ]]
  [[ "$(stat -c %u "$report")" == "$(id -u)" ]]
  [[ "$(stat -c %u "$outputs/private")" == "$(id -u)" ]]
  [[ "$(stat -c %u "$outputs/private/log")" == "$(id -u)" ]]
  [[ "$(stat -c %a "$outputs/private")" == 700 ]]
  [[ "$(stat -c %u "$outside")" == 0 ]]
  [[ -L "$outputs/link" ]]
done
