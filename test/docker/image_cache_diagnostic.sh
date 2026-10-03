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
test "$#" -eq 4
make=$(realpath "$1")
makefile=$(realpath "$2")
contexts=$(realpath "$3")
arm_archive=$(realpath "$4")
out="${TEST_UNDECLARED_OUTPUTS_DIR:?}/image-cache"
mkdir -p "$out"
work=$(mktemp -d "${TEST_TMPDIR:?}/image-cache.XXXXXX")
trap 'rm -rf "$work"' EXIT
tar --extract --file "$contexts" --directory "$work" \
  --same-permissions --no-same-owner
sha256sum "$make" "$makefile" "$contexts" "$arm_archive" > "$out/inputs.txt"
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

# Native cache hits and local-only misses share one registration graph node.
# Its native recipe is a no-op; this checks Make deduplication, not QEMU setup.
docker tag "$source_image" "$remote_image"
for name in basic_busybox basic_ubuntu; do
  missing_local=$("$make" "${make_args[@]}" "local-image-$name")
  missing_hash=${missing_local##*:}
  missing_remote="$remote_prefix/${name//_//}_$arch:$missing_hash"
  missing_latest="${missing_local%:*}:latest"
  printf '%s %s %s\n' "$missing_remote" "$missing_local" "$missing_latest" >> "$out/mixed-images.txt"
  if docker image inspect "$missing_remote" > "$out/$name-absent-remote.txt" 2>&1; then
    echo "Mixed-load fixture unexpectedly has a remote tag for $name" >&2
    exit 1
  fi
  docker tag "$source_image" "$missing_local"
done
"$make" "${make_args[@]}" --debug=b -j3 SKIP_IMAGE_LOAD=true \
  load-basic_alpine load-basic_busybox load-basic_ubuntu 2>&1 | tee "$out/mixed-load.txt"
test "$(grep -Fc "Successfully remade target file 'register-cross'." "$out/mixed-load.txt")" -eq 1
test "$(docker image inspect --format '{{.Id}}' "$local_image")" = "$image_id"
test "$(docker image inspect --format '{{.Id}}' "$latest")" = "$image_id"
while read -r missing_remote missing_local missing_latest; do
  test "$(docker image inspect --format '{{.Id}}' "$missing_local")" = "$image_id"
  for absent in "$missing_remote" "$missing_latest"; do
    if docker image inspect "$absent" >> "$out/mixed-absent-tags.txt" 2>&1; then
      echo "Mixed local-only load unexpectedly created $absent" >&2
      exit 1
    fi
  done
done < "$out/mixed-images.txt"
echo 'PASS mixed-load-shared-registration' | tee -a "$out/results.txt"

# Retag a real ARM64 archive on this AMD64 worker without executing it or
# visiting register-cross, regardless of the worker's existing binfmt state.
docker load --input "$arm_archive" > "$out/arm-load.txt"
arm_id=$(docker image inspect --format '{{.Id}}' "$source_image")
test "$(docker image inspect --format '{{.Architecture}}' "$source_image")" = arm64
arm_remote="$remote_prefix/basic/alpine_aarch64:$hash"
docker tag "$source_image" "$arm_remote"
printf 'id=%s\nremote=%s\nlocal=%s\nlatest=%s\n' \
  "$arm_id" "$arm_remote" "$local_image" "$latest" > "$out/arm-images.txt"
if docker image inspect multiarch/qemu-user-static > "$out/absent-registrar-before.txt" 2>&1; then
  echo 'Unexpected registrar image in the private daemon' >&2
  exit 1
fi
"$make" --no-print-directory -s -f "$makefile" --debug=b ARCH=aarch64 \
  "REMOTE_IMAGE_PREFIX=$remote_prefix" "LOCAL_IMAGE_PREFIX=$local_prefix" \
  SKIP_IMAGE_LOAD=true load-basic_alpine 2>&1 | tee "$out/arm-cache-hit.txt"
if grep -F register-cross "$out/arm-cache-hit.txt"; then
  echo 'Cached cross-architecture load visited register-cross' >&2
  exit 1
fi
test "$(docker image inspect --format '{{.Id}}' "$local_image")" = "$arm_id"
test "$(docker image inspect --format '{{.Id}}' "$latest")" = "$arm_id"
if docker image inspect multiarch/qemu-user-static > "$out/absent-registrar-after.txt" 2>&1; then
  exit 1
fi
echo 'PASS cross-cache-hit-without-registration' | tee -a "$out/results.txt"
if grep -E '^--- (PULL|REBUILD) ' "$out/mixed-load.txt" "$out/arm-cache-hit.txt"; then
  echo 'Cache/SKIP load unexpectedly attempted a pull or build' >&2
  exit 1
fi
