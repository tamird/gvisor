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

# Run a privileged Bazel test and return its declared reports to Bazel's user.
set -uo pipefail
[[ $# -gt 0 ]] || exit 2

caller="$(id -u):$(id -g)"
for output in "${XML_OUTPUT_FILE-}" "${TEST_UNDECLARED_OUTPUTS_DIR-}"; do
  case "$output" in
    "") ;;
    /|*/../*|*/..) echo "Invalid declared test output: $output" >&2; exit 1 ;;
    /*) ;;
    *) echo "Declared test output must be absolute: $output" >&2; exit 1 ;;
  esac
done

restore_outputs() {
  result=$?
  trap - EXIT HUP INT TERM
  cleanup_failed=0
  if [[ -n "${XML_OUTPUT_FILE-}" && ( -e "$XML_OUTPUT_FILE" || -L "$XML_OUTPUT_FILE" ) ]]; then
    sudo -n chown --no-dereference -- "$caller" "$XML_OUTPUT_FILE" || cleanup_failed=1
  fi
  if [[ -n "${TEST_UNDECLARED_OUTPUTS_DIR-}" && ( -e "$TEST_UNDECLARED_OUTPUTS_DIR" || -L "$TEST_UNDECLARED_OUTPUTS_DIR" ) ]]; then
    sudo -n chown --no-dereference --recursive -P -- "$caller" "$TEST_UNDECLARED_OUTPUTS_DIR" || cleanup_failed=1
  fi
  if [[ "$result" == 0 && "$cleanup_failed" != 0 ]]; then result=1; fi
  exit "$result"
}
trap restore_outputs EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

sudo -n -E "$@"
