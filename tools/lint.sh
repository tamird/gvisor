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

# Run the declared source checkers together; local --fix uses those same tools
# and indexed selections through host-configured Bazel applications.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

checks=()
fix=false
for arg in "$@"; do
  if [[ $arg == --fix ]]; then fix=true; else checks+=("$arg"); fi
done
if (( ${#checks[@]} == 0 )); then
  checks=(gofmt clang-format cpplint buildifier spelling)
fi

targets=()
optional=()
for check in "${checks[@]}"; do
  case "$check" in
    gofmt|clang-format|buildifier)
      targets+=("//tools/lint:$check") ;;
    cpplint|spelling)
      if ! "$fix"; then targets+=("//tools/lint:$check"); fi ;;
    actions|clang-tidy)
      if ! "$fix"; then optional+=("$check"); fi ;;
    *) echo "lint: unknown check '$check'" >&2; exit 1 ;;
  esac
done
if "$fix" && (( ${#targets[@]} == 0 )); then
  echo 'lint: --fix applies only to gofmt clang-format buildifier' >&2
  exit 1
fi

status=0
if "$fix"; then
  for target in "${targets[@]}"; do
    bazel run "${target}_fix" || status=1
  done
elif (( ${#targets[@]} )); then
  bazel test --keep_going "${targets[@]}" || status=1
fi
# Optional checks still run after a failure in the ordinary checks.
for check in "${optional[@]+"${optional[@]}"}"; do
  case "$check" in
    actions)
      make test OPTIONS=--enable_runfiles TARGETS=//:github_actions_test || status=1 ;;
    clang-tidy)
      bazel build --aspects=//tools/clang_tidy:clang_tidy.bzl%clang_tidy \
        --output_groups=clang_tidy //test/... //tools/... || status=1 ;;
  esac
done
exit "$status"
