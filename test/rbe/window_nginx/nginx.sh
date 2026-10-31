#!/bin/bash
set -euo pipefail
if [[ ${1:-} != --private-mount ]]; then
  exec unshare --mount --propagation private -- "$0" --private-mount "$@"
fi
shift
[[ $# == 4 && $EUID == 0 && $(uname -m) == x86_64 ]]
before=$(realpath "$1")
after=$(realpath "$3")
pair=$(realpath "$4")
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
bash "$pair" "$dir" "$before" "$after"
