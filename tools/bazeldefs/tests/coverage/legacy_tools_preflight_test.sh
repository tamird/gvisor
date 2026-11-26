#!/usr/bin/env bash

# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -euo pipefail

if [[ $# != 5 ]]; then
  echo "Expected bootstrap archive, source archive, manifests and module proxy" >&2
  exit 1
fi
bootstrap="$1"
source_archive="$2"
go_mod=$(realpath "$3")
go_sum=$(realpath "$4")
module_proxy=$(dirname "$(realpath "$5")")
for tool in bash curl tar gzip patch unzip realpath grep paste gcc mktemp sha256sum; do
  if ! command -v "$tool" >/dev/null; then
    echo "Missing legacy coverage tool: $tool" >&2
    exit 1
  fi
done
[[ "$(uname -m)" == x86_64 ]]
[[ "$(gcc -dumpmachine)" == x86_64-linux-gnu ]]
grep -Fxq 'go 1.27.3' "$go_mod"
source_version="$(tar -xOzf "$source_archive" go/VERSION)"
[[ "${source_version%%$'\n'*}" == go1.27.3 ]]
sdk_dir="$(mktemp -d "$TEST_TMPDIR/legacy-sdk.XXXXXX")"
trap 'rm -rf "$sdk_dir"' EXIT
tar -xzf "$bootstrap" -C "$sdk_dir"
version="$(cd "$sdk_dir" && GOROOT="$sdk_dir/go" GOTOOLCHAIN=local GOENV=off GOWORK=off "$sdk_dir/go/bin/go" version)"
[[ "$version" == 'go version go1.27.3 linux/amd64' ]]
mkdir "$sdk_dir/work"
cp "$go_mod" "$go_sum" "$sdk_dir/work/"
export GOROOT="$sdk_dir/go" GOTOOLCHAIN=local GOENV=off GOWORK=off
export GOPATH="$sdk_dir/gopath" GOMODCACHE="$sdk_dir/modules" GOCACHE="$sdk_dir/cache"
export GOPROXY="file://$module_proxy" GOSUMDB=off GOVCS='*:off' GOFLAGS=-mod=readonly
(cd "$sdk_dir/work" && "$GOROOT/bin/go" mod download && "$GOROOT/bin/go" mod verify)
cmp "$go_mod" "$sdk_dir/work/go.mod"
cmp "$go_sum" "$sdk_dir/work/go.sum"
bootstrap_sha="$(sha256sum "$bootstrap")"
source_sha="$(sha256sum "$source_archive")"
printf '{"go_version":"go1.27.3","architecture":"amd64","required_tools_present":true,"bootstrap_sha256":"%s","source_sha256":"%s","sdk_built":false,"modules_downloaded":true}\n' "${bootstrap_sha%% *}" "${source_sha%% *}"
