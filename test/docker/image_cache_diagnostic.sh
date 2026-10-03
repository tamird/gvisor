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
test "$#" -eq 3
make=$(realpath "$1")
makefile=$(realpath "$2")
contexts=$(realpath "$3")
out="${TEST_UNDECLARED_OUTPUTS_DIR:?}/image-cache"
mkdir -p "$out"
work=$(mktemp -d "${TEST_TMPDIR:?}/image-cache.XXXXXX")
trap 'rm -rf "$work"' EXIT
tar --extract --file "$contexts" --directory "$work" \
  --same-permissions --no-same-owner
sha256sum "$make" "$makefile" "$contexts" > "$out/inputs.txt"
"$make" --version > "$out/make-version.txt"
docker version > "$out/docker-version.txt"
cd "$work"
export MAKE="$make"
arch=$(uname -m)
test "$arch" = x86_64
remote_prefix=lazy-cache-remote.invalid
local_prefix=lazy-cache-local.invalid
make_args=(--no-print-directory -s -f "$makefile" "ARCH=$arch"
  "REMOTE_IMAGE_PREFIX=$remote_prefix" "LOCAL_IMAGE_PREFIX=$local_prefix")
local_image=$("$make" "${make_args[@]}" local-image-basic_alpine)
hash=${local_image##*:}
remote_image="$remote_prefix/basic/alpine_$arch:$hash"
latest="$local_prefix/basic/alpine:latest"
source_image=gvisor.dev/images/basic/alpine:latest
image_id=$(docker image inspect --format '{{.Id}}' "$source_image")
printf 'source=%s\nid=%s\nremote=%s\nlocal=%s\nlatest=%s\n' \
  "$source_image" "$image_id" "$remote_image" "$local_image" "$latest" > "$out/images.txt"

# This is the fixture's existing private daemon. Only the declared Alpine
# archive is loaded; the following operations neither pull nor build images.
docker tag "$source_image" "$remote_image"
"$make" "${make_args[@]}" SKIP_IMAGE_LOAD=true load-basic_alpine 2>&1 | tee "$out/cache-hit.txt"
test "$(docker image inspect --format '{{.Id}}' "$local_image")" = "$image_id"
test "$(docker image inspect --format '{{.Id}}' "$latest")" = "$image_id"
echo 'PASS cache-hit-before-skip' | tee -a "$out/results.txt"

# An actual Docker tag error must fail the cached branch, not fall through to
# pulling or rebuilding. Docker repository names cannot contain uppercase.
tag_status=0
"$make" "${make_args[@]}" LOCAL_IMAGE_PREFIX=invalid/UPPER SKIP_IMAGE_LOAD=true \
  load-basic_alpine > "$out/tag-failure.txt" 2>&1 || tag_status=$?
test "$tag_status" -ne 0
grep -F 'must be lowercase' "$out/tag-failure.txt"
printf 'tag_failure_exit=%d\n' "$tag_status" > "$out/expected-failures.txt"
echo 'PASS cache-tag-failure' | tee -a "$out/results.txt"

docker image rm "$remote_image" "$latest"
"$make" "${make_args[@]}" SKIP_IMAGE_LOAD=true load-basic_alpine 2>&1 | tee "$out/local-only.txt"
test "$(docker image inspect --format '{{.Id}}' "$local_image")" = "$image_id"
if docker image inspect "$latest" > "$out/absent-latest.txt" 2>&1; then
  echo 'Local-only SKIP unexpectedly created the latest tag' >&2
  exit 1
fi
echo 'PASS local-only-skip' | tee -a "$out/results.txt"

docker image rm "$local_image"
missing_status=0
"$make" "${make_args[@]}" SKIP_IMAGE_LOAD=true load-basic_alpine \
  > "$out/missing-local.txt" 2>&1 || missing_status=$?
test "$missing_status" -ne 0
grep -F 'does not exist locally and SKIP_IMAGE_LOAD is set' "$out/missing-local.txt"
printf 'missing_local_exit=%d\n' "$missing_status" >> "$out/expected-failures.txt"
echo 'PASS missing-local-skip-failure' | tee -a "$out/results.txt"

if grep -E "^--- (PULL|REBUILD) " "$out/cache-hit.txt" "$out/tag-failure.txt" \
  "$out/local-only.txt" "$out/missing-local.txt"; then
  echo "Image loading unexpectedly fell through to pull or rebuild" >&2
  exit 1
fi
