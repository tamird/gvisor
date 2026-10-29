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
makefile=$2
shift 2
# Firecracker applies the requested disk capacity to its root filesystem;
# TEST_TMPDIR is in the action workspace, which has fixed writable slack.
work=$(mktemp -d /tmp/gvisor-release.XXXXXX)
trap 'rm -rf "$work"' EXIT
df -B1 "$work" "$TEST_TMPDIR"
mounts=(--volume="$work:/work"
  --volume="$(realpath "$makefile"):/workspace/Makefile:ro"
  --volume="$(realpath "$TEST_UNDECLARED_OUTPUTS_DIR"):/reports")
while IFS= read -r artifact; do
  case "$artifact" in
    debian/release-artifacts/x86_64/*|debian/release-artifacts/aarch64/*)
      destination=artifacts/${artifact#debian/release-artifacts/}
      ;;
    sandboxexec/sandbox/python/dist)
      destination=artifacts/python
      ;;
    *) printf 'Unexpected release artifact path: %s\n' "$artifact" >&2; exit 1 ;;
  esac
  mounts+=(--volume="$(realpath "$artifact"):/input/$destination:ro")
done < "$manifest"
for script in "$@"; do
  mounts+=(--volume="$(realpath "$script"):/workspace/tools/$(basename "$script"):ro")
done

# Only declared scripts and packages enter this container. In particular, there
# is no Git metadata or Python uploader; release tags are explicit test inputs.
docker run --rm --runtime=runc --network=none --user=0:0 --read-only \
  --tmpfs=/tmp "${mounts[@]}" gvisor.dev/images/release-tools:latest \
  tools/repository_test_inner.sh
du -sb "$work"
