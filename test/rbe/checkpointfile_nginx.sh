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
[[ $# == 4 && $EUID == 0 && $(uname -m) == x86_64 && $RUNTIME == runsc ]]
[[ ${DOCKER_HOST:?} == unix:///tmp/gvisor-docker-*/docker.sock ]]
daemon_config=${DOCKER_HOST#unix://}
daemon_config=${daemon_config%/docker.sock}/daemon.json
state=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["exec-root"] + "/containerd/daemon")' "$daemon_config")
[[ -d $state ]]
mkdir -p /run/containerd
mount --bind "$state" /run/containerd

binary=$(realpath "$1")
benchstat=$(realpath "$2")
reporter=$(realpath "$3")
archive=$(realpath "$4")
out=${TEST_UNDECLARED_OUTPUTS_DIR:?}
cpu=$(python3 -c 'import os; print(min(os.sched_getaffinity(0)))')
printf 'checkpointfile: arch=%s cpu=%s bundle_state=%s runtime=%s\n' "$(uname -m)" "$cpu" "$state" "$RUNTIME"
grep -m1 'model name' /proc/cpuinfo
sha256sum "$binary"
printf '%s  %s\n' 26d929dac24fcba6a0b22c60db59f27fca01e070be2977ab380413e2f7a59401 "$benchstat" | sha256sum -c -
python3 --version
docker image inspect gvisor.dev/images/benchmarks/nginx:latest > "$out/nginx-inspect.json"
python3 "$reporter" --image "$archive" "$out/nginx-inspect.json"
GO_TEST_WRAP=1 GO_TEST_RUN_FROM_BAZEL=1 TEST_BINARY="$binary" \
  XML_OUTPUT_FILE="$out/checkpointfile.xml" GOMAXPROCS=1 \
  taskset -c "$cpu" "$binary" '-test.run=^TestCheckpointWriter' -test.v '-test.bench=^BenchmarkCheckpointFile$' \
  -test.benchtime=5x -test.benchmem -test.cpu=1 -test.count=1 \
  -test-checkpoint=true -runtime="$RUNTIME" -config_path="$daemon_config" \
  | tee "$out/checkpointfile.log"

python3 "$reporter" "$out"
"$benchstat" "statefile=$out/statefile.bench" "caller-buffer=$out/callerbuffer.bench" | tee "$out/benchstat.txt"
