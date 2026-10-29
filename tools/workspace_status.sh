#!/bin/bash

# Copyright 2018 The gVisor Authors.
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

git_version() {
  git describe --always --tags --exclude 'release-*-staging' --abbrev=12 --dirty
}

# Release jobs can resolve the checkout once, before starting Bazel.
if [[ "${1:-}" == --label ]]; then
  git_version
  exit $?
fi

version=$(git_version 2>/dev/null || echo 0.0.0)
printf 'STABLE_VERSION %s\n' "$version"
