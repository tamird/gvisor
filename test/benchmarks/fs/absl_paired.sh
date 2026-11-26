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
if test "${1:-}" = --inside; then
  dockerfile=$2
  recorder=$3
  shift 3
  tag=gvisor.dev/images/benchmarks/absl:latest
  base=$(docker image inspect "$tag" --format '{{.Id}}')
  test "$base" = sha256:f98e15990ecac4f5a0b28753acece11c97efca4766bfb3472d50418c5ff23a7c
  work=$(mktemp -d "${TEST_TMPDIR:-/tmp}/absl-prefetch.XXXXXX")
  common=
  cleanup() {
    status=$?
    trap - EXIT
    docker tag "$base" "$tag" || status=1
    if test -n "$common"; then docker image rm "$common" || status=1; fi
    rm -rf "$work"
    exit "$status"
  }
  trap cleanup EXIT
  printf 'FROM %s\n' "$tag" > "$work/Dockerfile"
  sed -n '/^# Cache source downloads/,$p' "$dockerfile" >> "$work/Dockerfile"
  test "$(wc -l < "$work/Dockerfile")" -eq 6
  docker build --progress=plain --pull=false --tag "$tag" "$work"
  common=$(docker image inspect "$tag" --format '{{.Id}}')
  echo "ABSL_PREFETCH_BASE=$base ABSL_COMMON_IMAGE=$common"
  docker run --rm --network=none --runtime=runc "$tag" bash -euc '
    test "$(cat /root/.bazelrc)" = "common --repository_cache=/opt/bazel-repository-cache"
    test -d /opt/bazel-repository-cache
    test -z "$(find /root/.cache/bazel -type d -name execroot -print -quit)"
  '
  docker run --rm --network=none --runtime=runc "$tag" python3 -c '
import hashlib, pathlib
root = pathlib.Path("/opt/bazel-repository-cache")
files = sorted(path for path in root.rglob("*") if path.is_file())
digest = hashlib.sha256()
for path in files:
    digest.update(str(path.relative_to(root)).encode() + b"\0")
    digest.update(path.read_bytes())
print("ABSL_REPOSITORY_CACHE", len(files), digest.hexdigest(), flush=True)
assert len(files) == 124
assert digest.hexdigest() == "20c679e8b74953dad569745779c0b451e7fbf9d5d3b835db7f73ebebe45e7ead"
'
  sudo -E "$recorder" "$@"
  exit
fi
cd "$TEST_SRCDIR/$TEST_WORKSPACE"
host=$1
recorder=$2
products=$3
dockerfile=$4
shift 4
unset GITHUB_TOKEN GH_TOKEN BUILDBUDDY_API_KEY
"$recorder" -validate "$@"
"$host" --filesystem --compare --products "$products" -- \
  /bin/bash "$0" --inside "$dockerfile" "$recorder" "$@"
