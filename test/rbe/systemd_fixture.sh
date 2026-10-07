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

# Run the full root suite with a native systemd cgroup manager.
set -euo pipefail
test "$#" -eq 4
out=${TEST_UNDECLARED_OUTPUTS_DIR:?}
stage=$(mktemp -d)
container=
docker_ready=false
cleanup() {
  status=$?
  trap - EXIT
  set +e
  if [[ -n ${container} ]]; then
    docker logs "${container}" > "${out}/systemd.log" 2>&1
    docker exec "${container}" journalctl --no-pager -u docker -u containerd \
      > "${out}/inner-docker.log" 2>&1
    docker exec "${container}" bash -c '
      for file in /tmp/runsc.*.log; do
        if [[ -f $file ]]; then printf "\\n%s\\n" "$file"; cat "$file"; fi
      done
    ' > "${out}/runsc.log" 2>&1
    if ${docker_ready}; then
      docker exec "${container}" systemctl stop docker.service docker.socket containerd.service || status=1
    fi
    docker stop --time=20 "${container}" || status=1
    docker rm --force --volumes "${container}" || status=1
  fi
  rm -rf "${stage}" || status=1
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Materialize only declared runfiles; inner processes cannot follow host links.
cp -LR release "${stage}/runtime"
cp -L "$1" "${stage}/root_test"
cp -L "$2" "${stage}/configure_runtime"
test -x "${stage}/runtime/runsc"
test -d "${stage}/runtime/gvisor-bin"
alpine=$(readlink -f "$3")
ubuntu=$(readlink -f "$4")
image=gvisor.dev/images/systemd-services:latest
docker image inspect "${image}" > "${out}/systemd-image.json"
test "$(docker info --format '{{.CgroupDriver}}/{{.CgroupVersion}}')" = cgroupfs/2

# Native runc owns the container subtree. Do not bind the worker's cgroup mount
# or use --init: systemd must be PID 1 in its private PID/cgroup namespace.
container=$(docker create --runtime=runc --privileged --cgroupns=private \
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  --stop-signal=SIGRTMIN+3 --tmpfs /run --tmpfs /tmp \
  --volume /var/lib/docker --volume /var/lib/containerd \
  --mount "type=bind,src=${stage},dst=/fixture,readonly" \
  --mount "type=bind,src=${alpine},dst=/alpine.tar,readonly" \
  --mount "type=bind,src=${ubuntu},dst=/ubuntu.tar,readonly" \
  "${image}" /sbin/init)
docker start "${container}"
pid=$(docker inspect --format '{{.State.Pid}}' "${container}")
test "$(readlink "/proc/${pid}/ns/cgroup")" != "$(readlink /proc/self/ns/cgroup)"
test "$(readlink "/proc/${pid}/ns/pid")" != "$(readlink /proc/self/ns/pid)"
test "$(readlink "/proc/${pid}/ns/mnt")" != "$(readlink /proc/self/ns/mnt)"
parent=$(basename "$(dirname "$(docker info --format '{{.DockerRootDir}}')")")
grep -Fx "0::/${parent}/${container}/init.scope" "/proc/${pid}/cgroup" || \
  grep -Fx "0::/${parent}/${container}" "/proc/${pid}/cgroup"

# Existing service readiness, with a finite deadline and no worker mutation.
timeout 120 docker exec "${container}" bash -c '
  until systemctl is-system-running --quiet; do sleep 0.2; done
'
docker exec -i "${container}" bash -se <<'SETUP'
set -euo pipefail
test "$(cat /proc/1/comm)" = systemd
test "$(stat -f -c %T /sys/fs/cgroup)" = cgroup2fs
test -w /sys/fs/cgroup/cgroup.subtree_control
systemctl --version
cat /proc/1/cgroup /sys/fs/cgroup/cgroup.controllers
# Use ordinary networking for the root tests, rather than the image's
# guest-Docker configuration that disables iptables.
cat > /etc/docker/daemon.json <<'CONFIG'
{"exec-opts":["native.cgroupdriver=systemd"],"storage-driver":"overlay2","debug":true}
CONFIG
/fixture/configure_runtime --runsc=/fixture/runtime/runsc --name=runsc \
  --config=/etc/docker/daemon.json -- --sidecar-usage-policy=STRICT \
  --debug --debug-log=/tmp/runsc.%TEST%.%TIMESTAMP%.%COMMAND%.log
systemctl start docker.service
test "$(docker info --format '{{.CgroupDriver}}/{{.CgroupVersion}}')" = systemd/2
docker info
docker load --input /alpine.tar
docker load --input /ubuntu.tar
SETUP
docker_ready=true

# Keep /proc, cgroup paths, Docker's PIDs and systemd's D-Bus PIDs in one view.
# The OOM tests inspect their parent. Keep a waiting shell inside this PID
# namespace; the direct docker exec process has an out-of-namespace parent.
# Move that process into a systemd scope before starting the shell, so neither
# the test nor its waiting parent blocks controller delegation at the root.
set +e
docker exec --env DOCKER_HOST=unix:///var/run/docker.sock \
  --env GVISOR_SIDECAR_BINARIES_DIR=/fixture/runtime/gvisor-bin \
  --env TEST_TIMEOUT="${TEST_TIMEOUT:?}" \
  "${container}" systemd-run --scope --quiet --unit=root-tests \
  --slice=system.slice --expand-environment=no bash -c '
    test "$(< /proc/self/cgroup)" = 0::/system.slice/root-tests.scope || exit 1
    mapfile -t processes < /sys/fs/cgroup/cgroup.procs || exit 1
    printf "Root-test membership: %s; root processes: %s\n" \
      "$(< /proc/self/cgroup)" "${processes[*]}"
    (( ${#processes[@]} == 0 )) || exit 1
    "$@"; exit "$?"
  ' systemd-root \
  /fixture/root_test --runtime=runsc \
  --config_path=/etc/docker/daemon.json -test.v \
  2>&1 | tee "${out}/root-test.log"
statuses=("${PIPESTATUS[@]}")
set -e
printf '%s\n' "${statuses[0]}" > "${out}/root-test-exit.txt"
test "${statuses[0]}" -eq 0
test "${statuses[1]}" -eq 0
for name in TestMemCgroup TestCgroupV2 TestCgroupParent TestSystemdCgroupJoinTwice; do
  grep -F -- "--- PASS: ${name} (" "${out}/root-test.log"
done
