#!/bin/bash
# Copyright 2026 The gVisor Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
set -euo pipefail
cd "$TEST_SRCDIR/$TEST_WORKSPACE"
profile=--filesystem
if [[ ${1:-} == --* ]]; then
  profile=$1
  shift
fi
host=$1
recorder=$2
products=$3
shift 3
unset GITHUB_TOKEN
"$recorder" -validate "$@"
"$host" "$profile" --compare --products "$products" -- sudo -E "$recorder" "$@"
