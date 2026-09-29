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

adapter=$1
export registry_versions=""
export registry_fails=0
export registry_calls="${TEST_TMPDIR}/registry-calls"

# Exercise the public adapter with deterministic external command responses.
date() { printf '%s\n' '2026.09.29'; }
gcloud() {
  printf '%s\n' "$*" >> "$registry_calls"
  if [[ $registry_fails == 1 ]]; then
    printf 'Registry unavailable\n' >&2
    return 1
  fi
  printf '%s\n' "$registry_versions"
}
export -f date gcloud

check_version() {
  local expected=$1 calls=$2
  shift 2
  : > "$registry_calls"
  local actual
  actual=$("$adapter" version "$@")
  if [[ "$actual" != "$expected" ]]; then
    printf 'version %s: got %s, want %s\n' "$*" "$actual" "$expected" >&2
    exit 1
  fi
  if [[ $(wc -l < "$registry_calls") -ne "$calls" ]]; then
    printf 'version %s: expected %s registry queries\n' "$*" "$calls" >&2
    exit 1
  fi
}

registry_versions=$'2026.9.28.9\n2026.9.29.2\n2026.9.29.10'
check_version 2026.9.29.11 1 auto
registry_versions=2026.9.28.9
check_version 2026.9.29.0 1 auto
registry_versions=""
check_version 2026.9.29.0 1 auto
check_version 2026.09.29.7 0 release-20260929.7
check_version 2026.09.29.8 0 2026.09.29.8

registry_fails=1
if actual=$("$adapter" version auto); then
  printf 'Failed registry query returned success: %s\n' "$actual" >&2
  exit 1
fi
if [[ -n "$actual" ]]; then
  printf 'Failed registry query returned a version: %s\n' "$actual" >&2
  exit 1
fi
check_version 2026.09.29.9 0 release-20260929.9
