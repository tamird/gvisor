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

# The benchmark reads OCI bundles at the system containerd path.
# Give its process that view of the test daemon without changing the daemon
# or the worker mount namespace.
if [[ ${1:-} != --private-mount ]]; then
  exec unshare --mount --propagation private -- "$0" --private-mount "$@"
fi
shift
[[ $# == 7 && $EUID == 0 && $(uname -m) == x86_64 && $RUNTIME == runsc ]]
[[ ${DOCKER_HOST:?} == unix:///tmp/gvisor-docker-*/docker.sock ]]
daemon_config=${DOCKER_HOST#unix://}
daemon_config=${daemon_config%/docker.sock}/daemon.json
state=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["exec-root"] + "/containerd/daemon")' "$daemon_config")
[[ -d $state ]]
mkdir -p /run/containerd
mount --bind "$state" /run/containerd

baseline=$(realpath "$1")
candidate=$(realpath "$2")
census=$(realpath "$3")
benchstat=$(realpath "$4")
reporter=$(realpath "$5")
archive=$(realpath "$6")
inputs=$(realpath "$7")
cpu=$(python3 -c 'import os; print(min(os.sched_getaffinity(0)))')
printf 'checkpointfile: arch=%s cpu=%s bundle_state=%s runtime=%s\n' "$(uname -m)" "$cpu" "$state" "$RUNTIME"
python3 "$reporter" --baseline "$baseline" --candidate "$candidate" --census "$census" \
  --benchstat "$benchstat" --archive "$archive" --inputs "$inputs" \
  --config "$daemon_config" --cpu "$cpu"
