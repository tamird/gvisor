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

# The unchanged benchmark reads OCI bundles at the system containerd path.
# Give its process that view of the test daemon without changing the daemon
# or the worker mount namespace.
if [[ ${1:-} != --private-mount ]]; then
  exec unshare --mount --propagation private -- "$0" --private-mount "$@"
fi
shift
[[ $# == 1 && $EUID == 0 && $(uname -m) == x86_64 && $RUNTIME == runsc ]]
[[ ${DOCKER_HOST:?} == unix:///tmp/gvisor-docker-*/docker.sock ]]
daemon_config=${DOCKER_HOST#unix://}
daemon_config=${daemon_config%/docker.sock}/daemon.json
state=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["exec-root"] + "/containerd/daemon")' "$daemon_config")
[[ -d $state ]]
mkdir -p /run/containerd
mount --bind "$state" /run/containerd

binary=$(realpath "$1")
out=${TEST_UNDECLARED_OUTPUTS_DIR:?}
printf 'checkpointfile: arch=%s bundle_state=%s runtime=%s\n' "$(uname -m)" "$state" "$RUNTIME"
sha256sum "$binary"
docker image inspect --format '{{.Id}}' gvisor.dev/images/benchmarks/nginx:latest
GO_TEST_WRAP=1 GO_TEST_RUN_FROM_BAZEL=1 TEST_BINARY="$binary" \
  XML_OUTPUT_FILE="$out/checkpointfile.xml" GOMAXPROCS=1 \
  "$binary" '-test.run=^$' '-test.bench=^BenchmarkCheckpointFile$' \
  -test.benchtime=1x -test.benchmem -test.cpu=1 -test.count=1 \
  -test-checkpoint=true -runtime="$RUNTIME" -config_path="$daemon_config" \
  | tee "$out/checkpointfile.log"

# A skipped benchmark or an empty test run can otherwise exit successfully.
awk '
  /^BenchmarkCheckpointFile\/Decode(-[0-9]+)?[[:space:]]/ && $2 == 1 {
    for (i = 4; i <= NF; i += 2)
      if ($i == "state-file-B" && $(i-1) > 0) ++decode
  }
  /^BenchmarkCheckpointFile\/Encode(-[0-9]+)?[[:space:]]/ && $2 == 1 {
    for (i = 4; i <= NF; i += 2)
      if ($i == "encoded-kernel-B" && $(i-1) > 0) ++encode
  }
  END { exit !(decode == 1 && encode == 1) }
' "$out/checkpointfile.log"
