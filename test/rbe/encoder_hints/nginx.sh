#!/bin/bash
set -euo pipefail
if [[ ${1:-} != --private-mount ]]; then
  exec unshare --mount --propagation private -- "$0" --private-mount "$@"
fi
shift
[[ $# == 2 && $EUID == 0 && $(uname -m) == x86_64 ]]
binary=$(realpath "$1")
archive=$(realpath "$2")
release=$(cd release && pwd)
test -x "$release/runsc"
export GVISOR_SIDECAR_BINARIES_DIR="$release/gvisor-bin"
test -x "$GVISOR_SIDECAR_BINARIES_DIR/gvisor_sentry"
dir=$(mktemp -d /tmp/gvisor-docker-XXXXXX)
export DOCKER_HOST="unix://$dir/docker.sock"
python3 - "$dir" "$release/runsc" <<'PY'
import json, pathlib, sys
d, binary = sys.argv[1:]
opts = {"hosts":["unix://"+d+"/docker.sock"],"data-root":d+"/data","exec-root":d+"/exec","pidfile":d+"/docker.pid","storage-driver":"overlay2","exec-opts":["native.cgroupdriver=cgroupfs"],"default-runtime":"runsc","experimental":True,"runtimes":{"runsc":{"path":binary,"runtimeArgs":["--shared-root="+d,"--platform=systrap","--debug=false"]}}}
pathlib.Path(d,"daemon.json").write_text(json.dumps(opts))
PY
dockerd --config-file "$dir/daemon.json" > "$dir/dockerd.log" 2>&1 &
daemon=$!
trap 'kill "$daemon" 2>/dev/null || true; wait "$daemon" 2>/dev/null || true' EXIT
for i in $(seq 1 180); do
  if docker info > /dev/null 2>&1; then break; fi
  if ! kill -0 "$daemon" 2>/dev/null; then tail -20 "$dir/dockerd.log"; exit 1; fi
  sleep 1
done
docker info --format 'restore-phases docker={{.ServerVersion}} experimental={{.ExperimentalBuild}}'
docker load -i "$archive"
tag=gvisor.dev/images/benchmarks/nginx:latest
[[ $(docker image inspect --format '{{.Id}}' "$tag") == sha256:5f9e05caa37420ad50b7ccf873ea117fcbd8cff24b3fcb689cb904bcab624cf5 ]]
test -d "$dir/exec/containerd/daemon"
mkdir -p /run/containerd
mount --bind "$dir/exec/containerd/daemon" /run/containerd
export RUNTIME=runsc
export RESTORE_PHASE_DIR="$dir/checkpoint"
install -d -m 700 "$RESTORE_PHASE_DIR"
export GOMAXPROCS=2
sha256sum "$release/runsc" "$binary"
"$binary" -test.v -test.run='^$' -test.bench='^BenchmarkCaptureRestorePhases$' -test.benchtime=1x -test.count=1 -test-checkpoint=true -runtime=runsc -config_path="$dir/daemon.json"
# Start each measured Save in a fresh process. The two arms differ only in
# whether they retain the kernel sizing hints supplied by the setup Load.
export GO_TEST_WRAP=0 GOMAXPROCS=1
cpu=$(python3 -c 'import os; print(min(os.sched_getaffinity(0)))')
for ((pair=0; pair<12; pair++)); do
  if ((pair % 2)); then arms=(clear loaded); else arms=(loaded clear); fi
  for mode in Encode EncodeStatefile; do
    for arm in "${arms[@]}"; do
      opts=(-probe-kernel-sizing)
      if [[ $arm == clear ]]; then opts+=(-clear-kernel-hints); fi
      child="$dir/$pair-$mode-$arm.log"
      taskset -c "$cpu" "$binary" -test.v -test.run='^$' -test.cpu=1 -test.bench="^BenchmarkCheckpointFile$/^${mode}$" \
        -test.benchtime=1x -test.count=1 -test.benchmem -checkpoint-dir="$RESTORE_PHASE_DIR" "${opts[@]}" > "$child" 2>&1 || {
          grep -E 'checkpointfile.go:[0-9]+:|^--- FAIL:|^FAIL$' "$child" | tail -5
          exit 1
        }
      grep -qx PASS "$child"
      printf 'SIZE_PAIR %d %s %s %s | %s\n' "$pair" "$arm" "$mode" \
        "$(grep 'KERNEL_SIZING bytes=' "$child" | tail -1)" \
        "$(grep -E "^BenchmarkCheckpointFile/${mode}(-[0-9]+)?[[:space:]]+1[[:space:]]" "$child")"
    done
  done
done
sha256sum "$RESTORE_PHASE_DIR/checkpoint.img"
