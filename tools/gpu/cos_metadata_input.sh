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

# Run a command from the repository root with the caller's complete COS catalog
# declared as test data. A private directory prevents overlapping invocations
# from replacing an input while Bazel is using it.
with_cos_metadata_input() (
  local source=$1 input_dir=test/gpu/cos_metadata_input status
  shift
  if [[ ! -f $source || ! -r $source ]]; then
    printf 'COS metadata requires a readable catalog file: %s\n' "$source" >&2
    return 2
  fi
  if ! mkdir "$input_dir"; then
    printf 'Cannot own COS metadata input directory: %s\n' "$input_dir" >&2
    return 2
  fi
  trap '
    status=$?
    if ! rm -f "$input_dir/images.json" || ! rmdir "$input_dir"; then
      if (( status == 0 )); then status=1; fi
    fi
    exit "$status"
  ' EXIT
  cp -- "$source" "$input_dir/images.json" || return
  "$@"
)
