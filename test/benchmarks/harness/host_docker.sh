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
# --startup selects the declared images and Docker 27 checkpoint-compatible profile.
# --network selects the declared networking images without changing the engine.
# --memory selects the declared memory benchmark images without changing the engine.
# --syscall selects the declared syscall image without changing the engine.
# --filesystem selects the declared filesystem images without changing the engine.
# --compare adds the declared baseline release with separate owned runtime names.
# Docker 28 default-network restores fail: https://github.com/moby/moby/issues/50750
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
  loaders+=(
    images/benchmark_nginx_load.exe
    images/benchmark_node_load.exe
    images/benchmark_util_load.exe
    images/basic/benchmark_redis_load.exe
  )
fi
network_mode=false
if test "${1:-}" = --network; then
  shift
  network_mode=true
  for name in nginx node iperf httpd ruby hey util; do
    names+=("$name")
    loaders+=("images/benchmark_${name}_load.exe")
  done
  names+=(redis)
  loaders+=(images/basic/benchmark_redis_load.exe)
fi
memory_mode=false
if test "${1:-}" = --memory; then
  shift
  memory_mode=true
  for name in nginx node sysbench util; do
    names+=("$name")
    loaders+=("images/benchmark_${name}_load.exe")
  done
  names+=(redis)
  loaders+=(images/basic/benchmark_redis_load.exe)
fi
syscall_mode=false
if test "${1:-}" = --syscall; then
  shift
  syscall_mode=true
  names+=(syscallbench)
  loaders+=(images/benchmarks/syscallbench/syscallbench_load.exe)
fi
filesystem_mode=false
if test "${1:-}" = --filesystem; then
  shift
  filesystem_mode=true
  names+=(fio absl build-grpc rubydev)
  loaders+=(
    images/benchmark_fio_load.exe
    images/benchmark_absl_load.exe
    images/benchmark_build_grpc_load.exe
    images/benchmark_rubydev_load.exe
  )
fi
comparison=false
if test "${1:-}" = --compare; then
  shift
  if ! $checkpoint_mode && ! $network_mode && ! $memory_mode && ! $syscall_mode && ! $filesystem_mode; then
    echo "--compare requires --startup, --network, --memory, --syscall, or --filesystem" >&2
    exit 1
  fi
  comparison=true
fi
repository_file() {
  python3 - "$RUNFILES_DIR/_repo_mapping" "$RUNFILES_DIR" "$1" "$2" <<'PY'
import csv, pathlib, sys
with open(sys.argv[1]) as mapping:
    repos = [row[2] for row in csv.reader(mapping)
             if row[1] == sys.argv[3] and row[0] in ("", "_main")]
assert len(repos) == 1, repos
print(pathlib.Path(sys.argv[2], repos[0], sys.argv[4]))
PY
}
image=gvisor.dev/images/benchmarks/alpine:latest
sudo -n true
docker info >/dev/null
# Removing our last tag must not delete an image that was already dangling.
if test -n "$(docker image ls -q --filter dangling=true)"; then
  echo "HOST_DOCKER_UNSUPPORTED: dangling images must be removed before admission" >&2
  exit 1
fi
initial_containers=$(docker ps -aq)
test -z "$initial_containers" # Require an otherwise unused daemon.
initial_experimental=$(docker info --format '{{.ExperimentalBuild}}')
restart_experimental=false
if $checkpoint_mode && test "$initial_experimental" = false; then
  restart_experimental=true
fi
initial_runtimes=$(docker info --format '{{json .Runtimes}}')
docker_root=$(docker info --format '{{.DockerRootDir}}')
if $comparison; then
  sudo python3 - "$initial_runtimes" "$docker_root/runtimes" "$config" <<'PY'
import json, pathlib, sys
configured = json.loads(sys.argv[1])
config_path = pathlib.Path(sys.argv[3])
on_disk = json.loads(config_path.read_text()).get("runtimes", {}) if config_path.exists() else {}
for name in ("benchmark-baseline", "benchmark-candidate"):
    if name in configured or name in on_disk or any(pathlib.Path(sys.argv[2]).glob(name + ".*")):
        raise SystemExit(f"Comparison runtime name is already in use: {name}")
PY
  baseline_archive=$(repository_file startup_baseline_release baseline-release.tar.bz2)
  test -f "$baseline_archive"
fi
engine_changed=false
if $checkpoint_mode; then
  initial_engine=$(docker version --format '{{.Server.Version}}')
  initial_driver=$(docker info --format '{{.Driver}}')
  initial_inventory=$(
    docker image ls --no-trunc --format '{{.ID}} {{.Repository}}:{{.Tag}}' | sort
  )
  initial_socket=$(stat -Lc '%f:%u:%g' /var/run/docker.sock)
  initial_socket_unit=$(systemctl show docker.socket --property=Listen,FragmentPath,DropInPaths)
  initial_containerd=$(systemctl show containerd --property=MainPID --value)
  # Keep the compatibility profile bound to the qualified host unit and versions.
  if test "$initial_engine" != 28.0.4; then
    echo "Unsupported checkpoint-profile host Docker version: $initial_engine" >&2
    exit 1
  fi
  test "$(systemctl show docker --property=DropInPaths --value)" = ''
  test "$(systemctl show docker --property=FragmentPath --value)" = /usr/lib/systemd/system/docker.service
  grep -qxF \
    'ExecStart=/usr/bin/dockerd -H fd:// --containerd=/run/containerd/containerd.sock' \
    /usr/lib/systemd/system/docker.service
fi
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
runtime_names=("$runtime")
release_roots=("$work/release")
if $comparison; then
  runtime_names=(benchmark-baseline benchmark-candidate)
  release_roots=("$work/baseline" "$work/release")
fi
owned_config="$work/daemon.json"
configured=()
loaded=()
dropin_dir=/run/systemd/system/docker.service.d
dropin="$dropin_dir/$runtime.conf"
dropin_dir_existed=false
if test -d "$dropin_dir"; then dropin_dir_existed=true; fi
# Roll back only to the original unit; never retry a failing temporary engine.
restart_original_service() {
  test "$(systemctl show docker --property=DropInPaths --value)" = '' || return 1
  if timeout 90s sudo systemctl restart docker; then return 0; fi
  systemctl show docker --property=Result,ExecMainStatus,StartLimitBurst,StartLimitIntervalUSec
  sudo journalctl -u docker.service --no-pager -n 20
  sudo systemctl reset-failed docker || return 1
  timeout 90s sudo systemctl restart docker
}
cleanup() {
  command_status=$?
  ret=0
  trap - EXIT
  # The daemon started empty and is exclusively owned for this invocation.
  if ! timeout 15s docker ps -aq | xargs -r timeout 30s docker rm -f >/dev/null; then
    if $engine_changed; then
      systemctl show docker --property=Result,ExecMainStatus,StartLimitBurst,StartLimitIntervalUSec
      sudo journalctl -u docker.service --no-pager -n 20
      # Keep the runtime/config intact while recovering the API and containers.
      if ! sudo rm -f "$dropin" || ! sudo systemctl daemon-reload ||
          ! restart_original_service || ! timeout 15s docker info >/dev/null; then
        echo "HOST_DOCKER_CLEANUP_BLOCKED: original service recovery failed; preserving $work" >&2
        exit 1
      fi
      echo HOST_DOCKER_ORIGINAL_SERVICE_RECOVERED
    fi
    if ! timeout 15s docker ps -aq | xargs -r timeout 30s docker rm -f >/dev/null; then
      echo "HOST_DOCKER_CLEANUP_BLOCKED: containers remain; preserving $work" >&2
      exit 1
    fi
  fi
  if test "${#configured[@]}" -gt 0; then
    # Canonical uninstall leaves an explicit runtime map for daemon reload.
    for i in "${configured[@]}"; do
      # Install can fail after writing its entry but before installing sidecars.
      runtime_present=$(sudo python3 - "$owned_config" "${runtime_names[$i]}" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
print(path.exists() and sys.argv[2] in json.loads(path.read_text()).get("runtimes", {}))
PY
      ) || {
        echo "HOST_DOCKER_CLEANUP_BLOCKED: cannot read owned config; preserving $work" >&2
        exit 1
      }
      if test "$runtime_present" = False; then continue; fi
      if ! sudo "${release_roots[$i]}/runsc" uninstall \
          --config_file="$owned_config" --runtime="${runtime_names[$i]}"; then
        echo "HOST_DOCKER_CLEANUP_BLOCKED: uninstall failed; preserving $work" >&2
        exit 1
      fi
    done
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
    if $engine_changed; then
      sudo rm -f "$dropin" || ret=1
      sudo systemctl daemon-reload || ret=1
    fi
    if $restart_experimental || $engine_changed; then
      restart_original_service || ret=1
    else
      sudo systemctl reload docker || ret=1
    fi
    for i in {1..10}; do
      if test "$(docker info --format '{{json .Runtimes}}')" = "$initial_runtimes" &&
          test "$(docker info --format '{{.ExperimentalBuild}}')" = "$initial_experimental"; then
        break
      fi
      sleep 1
    done
    if $config_existed; then
      sudo cp --preserve=all "$work/daemon.before" "$config" || ret=1
    else
      sudo rm -f "$config" || ret=1
    fi
  fi
  test "$(docker info --format '{{json .Runtimes}}')" = "$initial_runtimes" || ret=1
  test "$(docker ps -aq)" = "$initial_containers" || ret=1
  test "$(docker info --format '{{.ExperimentalBuild}}')" = "$initial_experimental" || ret=1
  if $checkpoint_mode; then
    test "$(docker version --format '{{.Server.Version}}')" = "$initial_engine" || ret=1
    test "$(docker info --format '{{.DockerRootDir}}')" = "$docker_root" || ret=1
    test "$(docker info --format '{{.Driver}}')" = "$initial_driver" || ret=1
    test "$(stat -Lc '%f:%u:%g' /var/run/docker.sock)" = "$initial_socket" || ret=1
    test "$(systemctl show docker.socket --property=Listen,FragmentPath,DropInPaths)" = "$initial_socket_unit" || ret=1
    test "$(systemctl show containerd --property=MainPID --value)" = "$initial_containerd" || ret=1
    if $engine_changed; then
      test "$(systemctl show docker --property=DropInPaths --value)" = '' || ret=1
      if ! $dropin_dir_existed; then sudo rmdir "$dropin_dir" || ret=1; fi
    fi
  fi
  if test "$ret" -ne 0; then
    echo "HOST_DOCKER_CLEANUP_BLOCKED: daemon restoration failed; preserving $work" >&2
    exit 1
  fi
  sudo python3 - "$docker_root/runtimes" "${runtime_names[@]}" <<'PY' || ret=1
import pathlib, sys
for runtime in sys.argv[2:]:
    for path in pathlib.Path(sys.argv[1]).glob(runtime + ".*"):
        path.unlink()
PY
  for image in "${loaded[@]}"; do
    loaded_image=$(docker image inspect "$image" --format '{{.Id}}' 2>/dev/null || true)
    if test -n "${initial_image[$image]}"; then
      docker tag "${initial_image[$image]}" "$image" || ret=1
    elif test -n "$loaded_image"; then
      docker image rm "$image" >/dev/null || ret=1
    fi
    if test -n "$loaded_image" &&
        ! grep -qxF "$loaded_image" <<<"$initial_images" &&
        test -n "${initial_image[$image]}"; then
      docker image rm "$loaded_image" >/dev/null || ret=1
    fi
    test "$(docker image inspect "$image" --format '{{.Id}}' 2>/dev/null || true)" = "${initial_image[$image]}" || ret=1
  done
  if $checkpoint_mode; then
    test "$(docker image ls --no-trunc --format '{{.ID}} {{.Repository}}:{{.Tag}}' | sort)" = "$initial_inventory" || ret=1
  fi
  if test "$ret" -ne 0; then
    echo "HOST_DOCKER_CLEANUP_BLOCKED: image restoration failed; preserving $work" >&2
    exit 1
  fi
  if $checkpoint_mode; then
    echo "HOST_DOCKER_ORIGINAL_ENGINE_RESTORED=$initial_engine"
  fi
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
if $comparison; then
  mkdir "$work/baseline"
  tar -xjf "$baseline_archive" -C "$work/baseline"
fi
for i in "${!release_roots[@]}"; do
  root=${release_roots[$i]}
  chmod -R a+rx "$root"
  prefix=HOST_DOCKER_RELEASE
  if $comparison && test "$i" -eq 0; then prefix=HOST_DOCKER_BASELINE_RELEASE; fi
  python3 - "$root" "$prefix" <<'PY'
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
for path in sorted(root.rglob("*")):
    if path.is_file():
        print(sys.argv[2], path.relative_to(root), hashlib.sha256(path.read_bytes()).hexdigest())
PY
  "$root/runsc" --version
  configured+=("$i")
  sudo "$root/runsc" install --experimental="$restart_experimental" \
    --config_file="$owned_config" --runtime="${runtime_names[$i]}" -- --platform=systrap
done
if $checkpoint_mode; then
  engine=$(repository_file docker_27_5_1 dockerd)
  cp "$engine" "$work/dockerd"
  chmod 0755 "$work/dockerd"
  test ! -e "$dropin"
  sudo mkdir -p "$dropin_dir"
  engine_changed=true
  printf '[Service]\nExecStart=\nExecStart=%s -H fd:// --containerd=/run/containerd/containerd.sock\n' \
    "$work/dockerd" | sudo tee "$dropin" >/dev/null
  sudo systemctl daemon-reload
fi
sudo cp "$owned_config" "$config"
if $restart_experimental || $engine_changed; then
  timeout 90s sudo systemctl restart docker
else
  sudo systemctl reload docker
fi
if $checkpoint_mode; then
  test "$(docker version --format '{{.Server.Version}}')" = 27.5.1
  echo HOST_DOCKER_CHECKPOINT_ENGINE=27.5.1
fi
if $checkpoint_mode; then
  test "$(docker info --format '{{.ExperimentalBuild}}')" = true
fi
for i in "${!runtime_names[@]}"; do
  for attempt in {1..10}; do
    actual=$(docker info --format "{{with index .Runtimes \"${runtime_names[$i]}\"}}{{.Path}}{{end}}")
    test "$actual" = "${release_roots[$i]}/runsc" && break
    sleep 1
  done
  test "$actual" = "${release_roots[$i]}/runsc"
  echo "HOST_DOCKER_RUNTIME_PATH ${runtime_names[$i]} $actual"
done
export RUNTIME="$runtime"
if $comparison; then export RUNTIME=benchmark-candidate; fi
if test "$#" -eq 0 || $comparison; then
  for selected in runc "${runtime_names[@]}"; do
    docker create --name "$container" --runtime="$selected" --network=none "$image" true >/dev/null
    test "$(docker inspect "$container" --format '{{.HostConfig.Runtime}}')" = "$selected"
    timeout 90s docker start -a "$container"
    docker rm "$container" >/dev/null
    echo "HOST_DOCKER_RUNTIME_PASS=$selected"
  done
fi
if test "$#" -gt 0; then
  "$@" --config_path="$config"
fi
