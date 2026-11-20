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
for variable in XML_OUTPUT_FILE TEST_UNDECLARED_OUTPUTS_DIR; do
  output="${!variable-}"
  case "$output" in
    "") ;;
    /|*/../*|*/..) echo "Invalid declared test output: $output" >&2; exit 1 ;;
    /*)
      output="$(realpath --canonicalize-missing --no-symlinks -- "$output")" || exit 1
      [[ "$output" != / ]] || { echo "Invalid declared test output: $output" >&2; exit 1; }
      printf -v "$variable" '%s' "$output"
      ;;
    *) echo "Declared test output must be absolute: $output" >&2; exit 1 ;;
  esac
done

restore_outputs() {
  result=$?
  trap - EXIT
  trap '' HUP INT TERM
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

forward_signal() {
  trap '' HUP INT TERM
  sudo -n kill -s "$1" -- "$child" 2>/dev/null || true
  wait "$child"
  exit "$2"
}
pending_signal=""
pending_status=0
trap 'pending_signal=HUP; pending_status=129' HUP
trap 'pending_signal=INT; pending_status=130' INT
trap 'pending_signal=TERM; pending_status=143' TERM

sudo -n -E "$@" &
child=$!
trap 'forward_signal HUP 129' HUP
trap 'forward_signal INT 130' INT
trap 'forward_signal TERM 143' TERM
if [[ -n "$pending_signal" ]]; then forward_signal "$pending_signal" "$pending_status"; fi
wait "$child"
