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

if [[ ${1:-} == --child ]]; then
  test "$(id -u)" -eq 65534
  id
  # Docker socket access is intentional; this is not a Docker isolation test.
  sed -n '/^Uid:/p; /^Gid:/p; /^Groups:/p; /^CapInh:/p; /^CapPrm:/p; /^CapEff:/p; /^CapBnd:/p; /^CapAmb:/p' /proc/self/status
  awk '/^Cap(Prm|Eff):/ { if ($2 != "0000000000000000") exit 1 }' /proc/self/status
  work=$2
  export HOME="${work}/home" TMPDIR="${work}/tmp"
  export DOCKER_HOST=unix:///var/run/docker.sock
  unset DOCKER_CONTEXT DOCKER_CONFIG DOCKER_CERT_PATH DOCKER_TLS_VERIFY
  /bin/bash "${work}/builder.sh" "${work}/buildx" "${work}/digest" \
    --platform=linux/amd64 --file="${work}/images/basic/redis/Dockerfile" \
    --output="type=oci,tar=false,compression=gzip,force-compression=true,dest=${work}/layout" \
    "${work}/images/basic/redis"
  test -s "${work}/layout/index.json"
  # The helper owns only its task-specific builder and configuration directory.
  remaining=$(docker ps --all --quiet --filter name=buildx_buildkit_gvisor-source-)
  test -z "${remaining}"
  remaining=$(docker volume ls --quiet --filter name=buildx_buildkit_gvisor-source-)
  test -z "${remaining}"
  shopt -s nullglob
  remaining=("${TMPDIR}"/source-image-builder.*)
  test "${#remaining[@]}" -eq 0
  exit 0
fi

test "$#" -eq 4
test "$(id -u)" -eq 0
setpriv=$(command -v setpriv)
"${setpriv}" --version
sha256sum "${setpriv}"
test -S /var/run/docker.sock
stat -c 'Docker socket mode=%a uid=%u gid=%g' /var/run/docker.sock
socket_gid=$(stat -c %g /var/run/docker.sock)

work=$(mktemp -d /tmp/source-image-docker-user.XXXXXX)
# shellcheck disable=SC2329
cleanup() {
  local result=$?
  trap - EXIT
  rm -rf "${work}" || { if (( result == 0 )); then result=1; fi; }
  exit "${result}"
}
trap cleanup EXIT
# Copies make the exact declared inputs traversable without changing Bazel's
# input tree or depending on the test's root-owned temporary-directory modes.
cp "$0" "${work}/check.sh"
cp "$1" "${work}/builder.sh"
cp "$2" "${work}/buildx"
cp "$3" "${work}/digest"
sha256sum "$1" "$2" "$3" "${work}/builder.sh" "${work}/buildx" "${work}/digest"
tar --extract --file "$4" --directory "${work}" --same-permissions --no-same-owner
mkdir "${work}/home" "${work}/tmp"
chmod 0755 "${work}/buildx"
chown -R 65534:65534 "${work}"
set +e
"${setpriv}" --reuid=65534 --regid=65534 --groups="${socket_gid}" \
  --inh-caps=-all --ambient-caps=-all --bounding-set=-all \
  /bin/bash "${work}/check.sh" --child "${work}"
result=$?
set -e
if [[ -s ${work}/layout/index.json ]]; then
  cp "${work}/layout/index.json" "${TEST_UNDECLARED_OUTPUTS_DIR:?}/index.json" || {
    if (( result == 0 )); then result=1; fi
  }
fi
exit "${result}"
