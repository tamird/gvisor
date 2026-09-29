#!/bin/bash

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
manifest=$1
shift
work=$(mktemp -d "$TEST_TMPDIR/release.XXXXXX")
trap 'rm -rf "$work"' EXIT
mounts=(--volume="$work:/work"
  --volume="$(realpath "$TEST_UNDECLARED_OUTPUTS_DIR"):/reports")
while IFS= read -r artifact; do
  case "$artifact" in
    test/release/artifacts/amd64/*|test/release/artifacts/arm64/*) ;;
    *) printf 'Unexpected release artifact path: %s\n' "$artifact" >&2; exit 1 ;;
  esac
  mounts+=(--volume="$(realpath "$artifact"):/input/${artifact#test/release/}:ro")
done < "$manifest"
for script in "$@"; do
  mounts+=(--volume="$(realpath "$script"):/workspace/tools/$(basename "$script"):ro")
done

# Only declared scripts and packages enter this container. In particular, there
# is no Git metadata or publishing script, so make_release takes its master path.
docker run --rm --runtime=runc --network=none --user=0:0 --read-only \
  --tmpfs=/tmp "${mounts[@]}" gvisor.dev/images/release-tools:latest \
  tools/repository_test_inner.sh
