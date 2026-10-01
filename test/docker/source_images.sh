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

# Validation branch only: build the canonical harness and exercise loopback.
set -euo pipefail
make="${TEST_SRCDIR:?}/@@TOOLS@@/@@MAKE@@"
crane="${TEST_SRCDIR}/@@TOOLS@@/crane"
makefile="${PWD}/@@MAKEFILE@@"
contexts="${PWD}/@@CONTEXTS@@"
out=${TEST_UNDECLARED_OUTPUTS_DIR:?}
work=$(mktemp -d "${TEST_TMPDIR:?}/cni-loopback.XXXXXX")
trap 'rm -rf "${work}"' EXIT
if [[ -n ${TEST_SHARD_STATUS_FILE:-} ]]; then
  touch "${TEST_SHARD_STATUS_FILE}"
fi
test "${TEST_TOTAL_SHARDS:-1}" -eq 1

tar --extract --file "${contexts}" --directory "${work}" \
  --same-permissions --no-same-owner
printf -v crane_command '%q' "${crane}"
export MAKE="${make}"
cd "${work}"
"${make}" -f "${makefile}" ARCH=@@ARCH@@ CRANE="${crane_command}" \
  load-containerd_harness
image=$("${make}" --no-print-directory -s -f "${makefile}" \
  ARCH=@@ARCH@@ CRANE="${crane_command}" local-image-containerd_harness)
printf '%s\n' "${image}" > "${out}/harness-image.txt"
docker image inspect "${image}" > "${out}/harness-image-inspect.json"

# Invoke the installed binary in the new image, outside each target namespace.
# This deliberately excludes bridge/iptables and the full containerd matrix.
docker run --rm --privileged --entrypoint /bin/bash -i "${image}" \
  -euo pipefail -s <<'LOOPBACK' | tee "${out}/loopback-output.txt"
plugin=/opt/cni/bin/loopback
version=$("${plugin}" 2>&1)
printf '%s\n' "${version}"
[[ "${version}" == *'CNI loopback plugin v1.9.1'* ]]
CNI_COMMAND=VERSION "${plugin}" </dev/null
for protocol in 0.3.1 1.0.0; do
  namespace="cni-loopback-${protocol}"
  ip netns add "${namespace}"
  trap 'ip netns del "${namespace}"' EXIT
  export CNI_CONTAINERID="${namespace}" CNI_NETNS="/var/run/netns/${namespace}"
  export CNI_IFNAME=lo CNI_PATH=/opt/cni/bin
  config="{\"cniVersion\":\"${protocol}\",\"name\":\"loopback-check\",\"type\":\"loopback\"}"
  before=$(ip -n "${namespace}" -o link show lo)
  printf 'Before ADD (%s): %s\n' "${protocol}" "${before}"
  [[ "${before}" != *'<LOOPBACK,UP'* ]]
  CNI_COMMAND=ADD "${plugin}" <<< "${config}"
  after=$(ip -n "${namespace}" -o link show lo)
  printf 'After ADD (%s): %s\n' "${protocol}" "${after}"
  [[ "${after}" == *'<LOOPBACK,UP,'* ]]
  ip -n "${namespace}" -4 -o addr show dev lo | tee /tmp/loopback-v4
  ip -n "${namespace}" -6 -o addr show dev lo | tee /tmp/loopback-v6
  grep -F 'inet 127.0.0.1/8' /tmp/loopback-v4
  grep -F 'inet6 ::1/128' /tmp/loopback-v6
  if [[ "${protocol}" == 1.0.0 ]]; then
    CNI_COMMAND=CHECK "${plugin}" <<< "${config}"
  fi
  CNI_COMMAND=DEL "${plugin}" <<< "${config}"
  after=$(ip -n "${namespace}" -o link show lo)
  printf 'After DEL (%s): %s\n' "${protocol}" "${after}"
  [[ "${after}" != *'<LOOPBACK,UP'* ]]
  ip netns del "${namespace}"
  trap - EXIT
  printf 'LOOPBACK_PROTOCOL_PASS %s\n' "${protocol}"
done
LOOPBACK

# Preserve the new image for a later authorized publication path; never push.
docker image save "${image}" --output "${out}/containerd-harness.tar"
sha256sum "${out}/containerd-harness.tar" > "${out}/containerd-harness.tar.sha256"
printf 'CNI_IMAGE_AND_LOOPBACK_PASS\n'
