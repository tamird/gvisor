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

# Installs an opaque release in the existing Docker daemon for one caller.
# Requires exclusive ownership of the host daemon for the entire invocation.
# With bazel test --run_under, use --noincompatible_bazel_test_exec_run_under
# to keep the complete release in the target configuration.
# --startup loads the maintained startup-category images and enables checkpoint API support.
# With no caller, checks the native and installed runtimes using Alpine.
set -euo pipefail
export RUNFILES_DIR="${TEST_SRCDIR:-${RUNFILES_DIR:-$0.runfiles}}"
runfiles="$RUNFILES_DIR/${TEST_WORKSPACE:-_main}"
config=/etc/docker/daemon.json
names=(alpine)
loaders=(images/benchmark_alpine_load.exe)
checkpoint_mode=false
if test "${1:-}" = --startup; then
  shift
  checkpoint_mode=true
  names+=(nginx node util redis)
  loaders+=(images/benchmark_nginx_load.exe images/benchmark_node_load.exe images/benchmark_util_load.exe images/basic/benchmark_redis_load.exe)
fi
image=gvisor.dev/images/benchmarks/alpine:latest
sudo -n true
docker info >/dev/null
initial_containers=$(docker ps -aq)
test -z "$initial_containers" # Require an otherwise unused daemon.
initial_experimental=$(docker info --format '{{.ExperimentalBuild}}')
restart_experimental=false
if $checkpoint_mode && test "$initial_experimental" = false; then restart_experimental=true; fi
initial_runtimes=$(docker info --format '{{json .Runtimes}}')
docker_root=$(docker info --format '{{.DockerRootDir}}')
work=$(mktemp -d "${TEST_TMPDIR:-${TMPDIR:-/tmp}}/benchmark-runtime.XXXXXX")
runtime=benchmark-$(basename "$work")
container=$runtime-check
config_existed=false
declare -A initial_image
for name in "${names[@]}"; do
  tag=gvisor.dev/images/benchmarks/$name:latest
  initial_image[$tag]=$(docker image inspect "$tag" --format '{{.Id}}' 2>/dev/null || true)
done
initial_images=$(docker image ls -aq --no-trunc)
mkdir "$work/release"
owned_config="$work/daemon.json"
configured=false
loaded=()
cleanup() {
  command_status=$?
  ret=0
  trap - EXIT
  # The daemon started empty and is exclusively owned for this invocation.
  if ! docker ps -aq | xargs -r docker rm -f >/dev/null; then
    echo "HOST_DOCKER_CLEANUP_BLOCKED: containers remain; preserving $work" >&2
    exit 1
  fi
  if $configured; then
    # Canonical uninstall leaves an explicit runtime map for daemon reload.
    if ! sudo "$work/release/runsc" uninstall --config_file="$owned_config" --runtime="$runtime"; then
      echo "HOST_DOCKER_CLEANUP_BLOCKED: uninstall failed; preserving $work" >&2
      exit 1
    fi
    if $restart_experimental; then
      sudo python3 - "$owned_config" <<'PYCONFIG' || ret=1
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
config = json.loads(path.read_text())
config["experimental"] = False
path.write_text(json.dumps(config))
PYCONFIG
    fi
    sudo cp "$owned_config" "$config" || ret=1
    if $restart_experimental; then timeout 90s sudo systemctl restart docker || ret=1; else sudo systemctl reload docker || ret=1; fi
    for i in {1..10}; do
      test "$(docker info --format '{{json .Runtimes}}')" = "$initial_runtimes" && test "$(docker info --format '{{.ExperimentalBuild}}')" = "$initial_experimental" && break
      sleep 1
    done
    if $config_existed; then sudo cp --preserve=all "$work/daemon.before" "$config" || ret=1; else sudo rm -f "$config" || ret=1; fi
  fi
  test "$(docker info --format '{{json .Runtimes}}')" = "$initial_runtimes" || ret=1
  test "$(docker ps -aq)" = "$initial_containers" || ret=1
  test "$(docker info --format '{{.ExperimentalBuild}}')" = "$initial_experimental" || ret=1
  if test "$ret" -ne 0; then
    echo "HOST_DOCKER_CLEANUP_BLOCKED: daemon restoration failed; preserving $work" >&2
    exit 1
  fi
  sudo python3 - "$docker_root/runtimes" "$runtime" <<'PY' || ret=1
import pathlib, sys
for path in pathlib.Path(sys.argv[1]).glob(sys.argv[2] + ".*"):
    path.unlink()
PY
  for image in "${loaded[@]}"; do
    loaded_image=$(docker image inspect "$image" --format '{{.Id}}' 2>/dev/null || true)
    if test -n "${initial_image[$image]}"; then
      docker tag "${initial_image[$image]}" "$image" || ret=1
    elif test -n "$loaded_image"; then
      docker image rm "$image" >/dev/null || ret=1
    fi
    if test -n "$loaded_image" && ! grep -qxF "$loaded_image" <<<"$initial_images" && test -n "${initial_image[$image]}"; then
      docker image rm "$loaded_image" >/dev/null || ret=1
    fi
    test "$(docker image inspect "$image" --format '{{.Id}}' 2>/dev/null || true)" = "${initial_image[$image]}" || ret=1
  done
  sudo rm -rf -- "$work" || ret=1
  echo "HOST_DOCKER_COMMAND_EXIT=$command_status"
  echo "HOST_DOCKER_CLEANUP_EXIT=$ret"
  if test "$ret" -ne 0; then exit "$ret"; fi
  exit "$command_status"
}
trap cleanup EXIT
if test -e "$config"; then
  # Snapshot target contents/metadata while leaving any daemon-config symlink intact.
  sudo cp -aL "$config" "$work/daemon.before"
  sudo cp -aL "$config" "$owned_config"
  config_existed=true
fi
for i in "${!names[@]}"; do
  loaded+=(gvisor.dev/images/benchmarks/${names[$i]}:latest)
  "$runfiles/${loaders[$i]}"
  docker image inspect "${loaded[$i]}" --format 'HOST_DOCKER_IMAGE {{.Id}} {{.Architecture}}'
done
# Preserve the complete producer-owned layout, including every sidecar.
cp -aL "$runfiles/release/." "$work/release/"
chmod -R a+rx "$work/release"
python3 - "$work/release" <<'PY'
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
for path in sorted(root.rglob("*")):
    if path.is_file():
        print("HOST_DOCKER_RELEASE", path.relative_to(root), hashlib.sha256(path.read_bytes()).hexdigest())
PY
"$work/release/runsc" --version
sudo "$work/release/runsc" install --experimental="$restart_experimental" --config_file="$owned_config" --runtime="$runtime" -- --platform=systrap
configured=true
sudo cp "$owned_config" "$config"
if $restart_experimental; then timeout 90s sudo systemctl restart docker; else sudo systemctl reload docker; fi
if $checkpoint_mode; then test "$(docker info --format '{{.ExperimentalBuild}}')" = true; fi
for i in {1..10}; do
  actual=$(docker info --format "{{with index .Runtimes \"$runtime\"}}{{.Path}}{{end}}")
  test "$actual" = "$work/release/runsc" && break
  sleep 1
done
test "$actual" = "$work/release/runsc"
export RUNTIME="$runtime"
if test "$#" -eq 0; then
  for selected in runc "$runtime"; do
    docker create --name "$container" --runtime="$selected" --network=none "$image" true >/dev/null
    test "$(docker inspect "$container" --format '{{.HostConfig.Runtime}}')" = "$selected"
    timeout 90s docker start -a "$container"
    docker rm "$container" >/dev/null
    echo "HOST_DOCKER_RUNTIME_PASS=$selected"
  done
else
  "$@" --config_path="$config"
fi
