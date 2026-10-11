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

if (( $# != 3 )); then
  echo "Usage: $0 RUNSC IMAGE_ARCHIVE|- IMAGE" >&2
  exit 2
fi
runsc=$(realpath "$1")
archive=$2
image=$3
if [[ $archive != - ]]; then
  archive=$(realpath "$archive")
fi
if (( $(id -u) == 0 )) || [[ $(stat -fc %T /sys/fs/cgroup) != cgroup2fs ]]; then
  echo 'The Podman smoke test requires an unprivileged user and cgroup v2.' >&2
  exit 1
fi
# Do not inherit the podmantest image's permissive ignore_chown_errors setting,
# or share a user's containers, pause process, image store or runtime state.
# Podman 3.4 limits runroot to 50 characters; TEST_TMPDIR can exceed that.
state=$(mktemp -d /tmp/gvisor-podman.XXXXXX)
export HOME="$state/home" XDG_CONFIG_HOME="$state/config"
export XDG_DATA_HOME="$state/data" XDG_RUNTIME_DIR="$state/runtime" TMPDIR="$state/tmp"
unset CONTAINERS_STORAGE_CONF CONTAINERS_CONF CONTAINER_HOST CONTAINER_CONNECTION
podman_args=(--root "$state/storage" --runroot "$state/runroot" --tmpdir "$state/libpod"
  --cgroup-manager=cgroupfs --runtime "$state/runsc")
initialized=false
cleanup() {
  local status=$?
  trap - EXIT
  if [[ $initialized == true ]]; then
    podman "${podman_args[@]}" rm --all --force || status=1
    podman "${podman_args[@]}" system reset --force || status=1
  fi
  if [[ -f $state/runsc.log && -n ${TEST_UNDECLARED_OUTPUTS_DIR:-} ]]; then
    cp "$state/runsc.log" "$TEST_UNDECLARED_OUTPUTS_DIR/runsc.log" || status=1
  fi
  rm -rf "$state" || status=1
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -m 700 "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_RUNTIME_DIR" "$TMPDIR"

case "${GVISOR_TEST_CLOCK_SOURCE:-}" in
  ""|calibrated|reference) ;;
  *) printf 'Invalid GVISOR_TEST_CLOCK_SOURCE: %s\n' "$GVISOR_TEST_CLOCK_SOURCE" >&2; exit 2 ;;
esac

# Keep the installed adapter's shell-fragment interface intact. Podman's
# runtime-flag prefixes each argument with "--", so it cannot carry arbitrary
# Go flag/value pairs. Only this unprivileged wrapper interprets RUNTIME_ARGS;
# paths and the OCI command arguments are passed intact.
{
  printf '#!/bin/bash\nexec %q --ignore-cgroups --debug --debug-log=%q --sidecar-usage-policy=STRICT ' "$runsc" "$state/runsc.log"
  if [[ -n ${GVISOR_TEST_CLOCK_SOURCE:-} ]]; then
    printf '%q ' "--clock-source=$GVISOR_TEST_CLOCK_SOURCE"
  fi
  printf '%s "$@"\n' "${RUNTIME_ARGS:-}"
} > "$state/runsc"
chmod 700 "$state/runsc"

# Without a legacy override, both adapters run the public DirectFS matrix.
# RUNTIME_ARGS retains the original single configured invocation semantics.
directfs_modes=(false true)
if [[ -n ${RUNTIME_ARGS:-} ]]; then
  directfs_modes=(selected)
fi

id
podman --version
initialized=true
[[ $(podman "${podman_args[@]}" info --format '{{.Host.Security.Rootless}}') == true ]]
podman "${podman_args[@]}" info --format 'storage={{.Store.GraphDriverName}}'
# A single-ID fallback is insufficient: the public lane exercises subordinate
# identities through rootlesskit-compatible mappings (c3abb8c00).
for kind in uid gid; do
  if [[ $kind == uid ]]; then
    caller_id=$(id -u)
  else
    caller_id=$(id -g)
  fi
  podman "${podman_args[@]}" unshare cat "/proc/self/${kind}_map" > "$state/${kind}_map"
  cat "$state/${kind}_map"
  awk -v caller="$caller_id" '$1 == 0 && $2 == caller && $3 == 1 { root = 1 }
       $1 > 0 && $3 > 1 { subordinate = 1 }
       END { exit !(root && subordinate) }' "$state/${kind}_map"
done
# Default rootless networking uses slirp4netns, which opens the host TUN device.
if ! stat -Lc '%n: type=%F mode=%a uid=%u gid=%g device=%t:%T' /dev /dev/net /dev/net/tun ||
   [[ ! -c /dev/net/tun || ! -r /dev/net/tun || ! -w /dev/net/tun ]]; then
  echo 'Rootless Podman networking requires a readable, writable /dev/net/tun.' >&2
  exit 1
fi
if [[ $archive == - ]]; then
  # The installed adapter retains its ordinary registry-backed image setup.
  podman "${podman_args[@]}" pull "$image"
else
  podman "${podman_args[@]}" load --input "$archive"
fi

status=0
for directfs in "${directfs_modes[@]}"; do
  mode_args=()
  if [[ $directfs != selected ]]; then
    mode_args=(--runtime-flag="directfs=$directfs")
  fi
  echo "Running rootless Podman with directfs=$directfs"
  if output=$(podman "${podman_args[@]}" "${mode_args[@]}" run --rm --pull=never "$image" echo 'Hello, world'); then
    if [[ $output != 'Hello, world' ]]; then
      printf 'Unexpected container output: %s\n' "$output" >&2
      status=1
    else
      echo "PASS: rootless Podman directfs=$directfs"
    fi
  else
    status=1
  fi
done
exit "$status"
