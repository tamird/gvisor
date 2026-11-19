#!/bin/bash

# Copyright 2025 The gVisor Authors.
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

set -euxo pipefail

today="$(date +"%Y-%m-%d")"
repo_url="https://github.com/gvisor/gvisor.git"
pgo_branch_name="pgo/update-${today}"

# GitHub CLI authentication.
gh --version
gh auth login --with-token < "$HOME/.github-token"
gh auth setup-git

exec "$(dirname "$0")/publish.sh" "$today" "$repo_url" "$pgo_branch_name"
