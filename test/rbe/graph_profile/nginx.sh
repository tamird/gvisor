#!/bin/bash
set -euo pipefail
if [[ ${1:-} != --private-mount ]]; then
  exec unshare --mount --propagation private -- "$0" --private-mount "$@"
fi
shift
[[ $# == 3 && $EUID == 0 && $(uname -m) == x86_64 ]]
binary=$(realpath "$1")
treatment=$(realpath "$2")
archive=$(realpath "$3")
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
export GO_TEST_WRAP=0 GOMAXPROCS=1
unset XML_OUTPUT_FILE
cpu=$(python3 -c 'import os; print(min(os.sched_getaffinity(0)))')
out=$TEST_UNDECLARED_OUTPUTS_DIR
mkdir -p "$dir/captured" "$out"
flags=(-test-checkpoint=true -runtime=runsc "-config_path=$dir/daemon.json" -test.cpu=1)
export CHECKPOINT_CAPTURE_DIR="$dir/captured"
taskset -c "$cpu" "$binary" -test.run='^$' -test.bench='^BenchmarkCheckpointFile$/^EncodeStatefile$' -test.benchtime=1x "${flags[@]}" > "$dir/capture.log" 2>&1 || { tail -22 "$dir/capture.log"; exit 1; }
unset CHECKPOINT_CAPTURE_DIR
(cd "$dir/captured"; sha256sum *.img spec.json; wc -c *.img spec.json) > "$out/input.txt"
sha256sum "$release/runsc" "$release/gvisor-bin/gvisor_sentry" "$binary" "$treatment" > "$out/binaries.txt"
flags+=("-checkpoint-dir=$dir/captured")
for arm in control candidate; do
  exe=$binary
  [[ $arm == control ]] || exe=$treatment
  taskset -c "$cpu" "$exe" -test.v -test.run='^TestCheckpointFileRoundTrip$' "${flags[@]}" > "$dir/$arm.check" 2>&1 || { tail -18 "$dir/$arm.check"; exit 1; }
  grep -E -- 'verified input graph header:|State file sizes:|--- PASS: TestCheckpointFileRoundTrip' "$dir/$arm.check" > "$out/$arm-roundtrip.txt"
  grep -q 'verified input graph header: kernel=true' "$out/$arm-roundtrip.txt"
  grep -q -- '--- PASS: TestCheckpointFileRoundTrip' "$out/$arm-roundtrip.txt"
done
for pair in {1..8}; do
  arms=(control candidate)
  if (( pair % 2 == 0 )); then arms=(candidate control); fi
  for arm in "${arms[@]}"; do
    exe=$binary
    [[ $arm == control ]] || exe=$treatment
    stem=$pair-$arm
    taskset -c "$cpu" "$exe" -test.run='^$' '-test.bench=^BenchmarkCheckpointFile$/^DecodeStatefile$' -test.benchtime=2s -test.benchmem "${flags[@]}" > "$dir/$stem.log" 2>&1 || { tail -18 "$dir/$stem.log"; exit 1; }
    grep -E '^BenchmarkCheckpointFile/DecodeStatefile(-[0-9]+)?[[:space:]]' "$dir/$stem.log" > "$out/$stem.bench"
    grep -qx PASS "$dir/$stem.log"
    echo "$pair $arm" >> "$out/order.txt"
  done
done
(cd "$dir/captured"; sha256sum -c <(head -4 "$out/input.txt")) > "$out/input-after.txt"
