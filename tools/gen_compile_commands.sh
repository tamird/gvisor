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

# Generate compile_commands.json for clangd and clang-tidy. Optional arguments
# are the Bazel executable and startup options, then -- and build options.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

bazel=()
while [[ "$#" -gt 0 ]]; do
  if [[ "$1" == -- ]]; then
    shift
    break
  fi
  bazel+=("$1")
  shift
done
if [[ "${#bazel[@]}" -eq 0 ]]; then
  bazel=(bazel)
fi
build_options=("$@")
targets=(//test/... //tools/...)
generator_options=(
  --resolve
  --replace=-fno-canonical-system-headers=
  "--bazel-command=${bazel[0]}"
)
for ((i = 1; i < ${#bazel[@]}; i++)); do
  generator_options+=("--bazelsupopt=${bazel[i]}")
done
for option in "${build_options[@]+"${build_options[@]}"}"; do
  generator_options+=("--bazelopt=${option}")
done

database="$(pwd)/compile_commands.json"
temporary_database="$(mktemp "${database}.XXXXXX")"
trap 'rm -f -- "${temporary_database}"' EXIT

# Remove a GCC-only flag that Clang does not accept. Pass the same executable
# and options to the generator's nested Bazel commands.
"${bazel[@]}" run \
  "${build_options[@]+"${build_options[@]}"}" --platforms=@platforms//host \
  //tools/compile_commands -- "${generator_options[@]}" \
  "--output=${temporary_database}" "${targets[@]}"

# Run preparation last: Bazel run can remove execution-root source symlinks.
# Download all outputs so an action that generates a header also materializes
# its sibling sources. The aspect prepares these inputs without compiling tests.
"${bazel[@]}" build \
  "${build_options[@]+"${build_options[@]}"}" --remote_download_outputs=all \
  --aspects=//tools:cc_compile_inputs.bzl%cc_compile_inputs \
  --output_groups=cc_compile_inputs "${targets[@]}"

# Keep the previous database if generation or input preparation fails.
mv -- "${temporary_database}" "${database}"
