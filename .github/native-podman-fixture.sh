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

# Bazel supplies the actual test executable and its declared runtime arguments.
# Run those in the same podmantest image and identity as the remote owner,
# without giving the nested runtime a host Docker socket or host credentials.
set -euo pipefail

# Qualify the runner's installed Podman before considering a native payload.
# This path uses the ordinary Actions user and its existing distribution setup.
if [[ $# == 1 && $1 == --host-preflight ]]; then
  [[ $(uname -m) == aarch64 && $(id -u) != 0 ]]
  uname -srmo
  id
  diagnostic() {
    "$@" || printf 'Diagnostic unavailable (exit %s): %s\n' "$?" "$*" >&2
  }
  diagnostic cat /proc/self/uid_map /proc/self/gid_map /proc/self/attr/current
  diagnostic grep -E '^(Uid|Gid|Cap|NoNewPrivs|Seccomp)' /proc/self/status
  for setting in /proc/sys/kernel/unprivileged_userns_clone \
    /proc/sys/kernel/apparmor_restrict_unprivileged_userns /proc/sys/user/max_user_namespaces; do
    if [[ -r $setting ]]; then printf '%s=' "$setting"; cat "$setting"; fi
  done
  diagnostic awk -F: -v user="$(id -un)" -v uid="$(id -u)" \
    '$1 == user || $1 == uid { print FILENAME ":" $0 }' /etc/subuid /etc/subgid
  diagnostic stat -Lc '%n mode=%a uid=%u gid=%g' /usr/bin/newuidmap /usr/bin/newgidmap /dev/fuse /dev/net/tun
  diagnostic sha256sum /usr/bin/newuidmap /usr/bin/newgidmap
  if ! command -v podman; then
    printf 'STOCK_PODMAN_PREFLIGHT status=UNAVAILABLE reason=podman-not-installed\n'
    exit 127
  fi
  podman --version
  sha256sum "$(command -v podman)"
  directory=$(mktemp -d "${RUNNER_TEMP:?}/stock-podman-preflight.XXXXXX")
  printf 'Stock Podman diagnostic directory: %s\n' "$directory"
  trap 'rm -rf -- "$directory"' EXIT
  status=0
  podman --log-level=debug info --format=json > "$directory/info.json" || status=$?
  cat "$directory/info.json"
  if (( status == 0 )) && ! jq -e '.host.security.rootless == true' "$directory/info.json"; then
    status=1
  fi
  for kind in uid gid; do
    if [[ $kind == uid ]]; then caller=$(id -u); else caller=$(id -g); fi
    mapping_status=0
    podman --log-level=debug unshare cat "/proc/self/${kind}_map" > "$directory/${kind}_map" || mapping_status=$?
    cat "$directory/${kind}_map"
    if (( mapping_status == 0 )) && ! awk -v caller="$caller" \
      '$1 == 0 && $2 == caller && $3 == 1 { root = 1 }
       $1 > 0 && $3 > 1 { subordinate = 1 }
       END { exit !(root && subordinate) }' "$directory/${kind}_map"; then
      mapping_status=1
    fi
    printf 'STOCK_PODMAN_MAPPING kind=%s exit=%s\n' "$kind" "$mapping_status"
    if (( status == 0 && mapping_status != 0 )); then status=$mapping_status; fi
  done
  printf 'STOCK_PODMAN_PREFLIGHT exit=%s finished=%s\n' "$status" "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)"
  exit "$status"
fi

execution=fixture
if [[ ${1:-} == --host-runtime ]]; then
  execution=host
  shift
fi

fixture_args=(--rm --platform=linux/arm64 --cgroupns=private --user nonroot:nonroot
  --device=/dev/fuse --device=/dev/net/tun --security-opt=seccomp=unconfined
  --security-opt=apparmor=unconfined --security-opt=systempaths=unconfined)

# Check the namespace prerequisite before building any runtime binaries. Use
# the same container profile as the actual owner, with no host credentials.
if [[ $# == 1 && $1 == --preflight ]]; then
  [[ $(uname -m) == aarch64 ]]
  printf 'HOST_NAMESPACE\n'
  cat /proc/self/uid_map /proc/self/gid_map
  for setting in /proc/sys/kernel/unprivileged_userns_clone \
    /proc/sys/kernel/apparmor_restrict_unprivileged_userns /proc/sys/user/max_user_namespaces; do
    if [[ -r $setting ]]; then printf '%s=' "$setting"; cat "$setting"; fi
  done
  # The embedded script expands these variables inside the fixture.
  # shellcheck disable=SC2016
  exec docker run "${fixture_args[@]}" --entrypoint=/bin/bash "${PODMAN_FIXTURE:?}" -ceu '
    [[ $(uname -m) == aarch64 && $(id -u) == 1338 && $(id -g) == 1337 ]]
    [[ $(stat -fc %T /sys/fs/cgroup) == cgroup2fs && -c /dev/fuse && -c /dev/net/tun ]]
    diagnostic() {
      "$@" || printf "Diagnostic unavailable (exit %s): %s\n" "$?" "$*" >&2
    }
    id
    podman --version
    printf "FIXTURE_NAMESPACE\n"
    diagnostic cat /proc/self/uid_map /proc/self/gid_map /proc/1/uid_map /proc/1/gid_map
    diagnostic readlink /proc/self/ns/user /proc/1/ns/user
    diagnostic cat /proc/self/attr/current
    diagnostic grep -E "^(Uid|Gid|Cap|NoNewPrivs|Seccomp)" /proc/self/status
    diagnostic cat /etc/subuid /etc/subgid
    diagnostic stat -Lc "%n mode=%a uid=%u gid=%g" /usr/bin/newuidmap /usr/bin/newgidmap
    diagnostic sha256sum /usr/bin/newuidmap /usr/bin/newgidmap
    diagnostic awk "\$2 == \"/\" { printf \"rootfs type=%s nosuid=%d\\n\", \$3, (index(\",\" \$4 \",\", \",nosuid,\") > 0) }" /proc/mounts
    status=0
    podman --log-level=debug info || status=$?
    printf "PODMAN_PREFLIGHT exit=%s finished=%s\n" "$status" "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)"
    exit "$status"'
fi

[[ $# == 4 ]]
case ${TEST_TARGET:?} in
  //test/podman:podman_test_arm64|@@//test/podman:podman_test_arm64) ;;
  *) printf 'Unexpected Podman test target: %s\n' "$TEST_TARGET" >&2; exit 1 ;;
esac
[[ ${GVISOR_TEST_CLOCK_SOURCE:?} == calibrated && $(uname -m) == aarch64 ]]
[[ $4 == gvisor.dev/images/basic/alpine:latest ]]
printf '%s  %s\n' "${PODMAN_ALPINE_SHA256:?}" "$3" | sha256sum -c -
for binary in "$2" "$(dirname "$2")"/gvisor-bin/*; do
  readelf -h "$binary" | grep -E 'Machine: +AArch64$'
  sha256sum "$binary"
done

if [[ $execution == host ]]; then
  [[ $(id -u) != 0 && $(stat -fc %T /sys/fs/cgroup) == cgroup2fs ]]
  [[ $(podman --version) == 'podman version 4.9.3' ]]
  printf 'HOST_PODMAN_IDENTITY arch=%s uid=%s gid=%s cgroup=%s\n' \
    "$(uname -m)" "$(id -u)" "$(id -g)" "$(stat -fc %T /sys/fs/cgroup)"
  sha256sum "$(command -v podman)"
  exec "$@"
fi

# Runfiles can refer outside the output tree through absolute symlinks.
# Materialize only this test's declared inputs for the unprivileged container.
stage=$(mktemp -d "${TEST_TMPDIR:?}/native-podman.XXXXXX")
cleanup() {
  local status=$?
  trap - EXIT
  # cp preserves Bazel's read-only directory modes on these task-local copies.
  if [[ -d $stage/runfiles ]] && ! chmod -R u+w "$stage/runfiles"; then
    printf 'Could not make staged runfiles writable for cleanup.\n' >&2
    if (( status == 0 )); then status=1; fi
  fi
  if ! rm -rf -- "$stage"; then
    printf 'Could not remove Podman staging directory: %s\n' "$stage" >&2
    if (( status == 0 )); then status=1; fi
  fi
  exit "$status"
}
trap cleanup EXIT
mkdir "$stage/runfiles" "$stage/output"
cp -aL -- "$TEST_SRCDIR/." "$stage/runfiles/"
chmod -R a+rX "$stage/runfiles"
chmod 0777 "$stage/output"
workdir=$(realpath --relative-to="$TEST_SRCDIR" "$PWD")
[[ $workdir != .. && $workdir != ../* ]]
arguments=()
for value do
  case "$value" in
    "$TEST_SRCDIR"/*) value=/qualification/runfiles/${value#"$TEST_SRCDIR"/} ;;
    /*) printf 'Unexpected absolute argument to native Podman test: %s\n' "$value" >&2; exit 1 ;;
  esac
  arguments+=("$value")
done
# Bash does not search the current working directory for an executable.
if [[ ${arguments[0]} != /* && ${arguments[0]} != ./* ]]; then arguments[0]=./${arguments[0]}; fi

status=0
# The embedded script expands these variables inside the fixture.
# shellcheck disable=SC2016
docker run "${fixture_args[@]}" \
  --mount "type=bind,src=$stage/runfiles,dst=/qualification/runfiles,readonly" \
  --mount "type=bind,src=$stage/output,dst=/qualification/output" \
  --workdir "/qualification/runfiles/$workdir" \
  --env GVISOR_TEST_CLOCK_SOURCE --env TEST_UNDECLARED_OUTPUTS_DIR=/qualification/output \
  --entrypoint=/bin/bash "${PODMAN_FIXTURE:?}" -ceu '
    [[ $(uname -m) == aarch64 && $(id -u) == 1338 && $(id -g) == 1337 ]]
    [[ $(stat -fc %T /sys/fs/cgroup) == cgroup2fs && -c /dev/fuse && -c /dev/net/tun ]]
    printf "FIXTURE_IDENTITY arch=%s uid=%s gid=%s cgroup=%s\n" \
      "$(uname -m)" "$(id -u)" "$(id -g)" "$(stat -fc %T /sys/fs/cgroup)"
    exec "$@"' -- "${arguments[@]}" || status=$?
if [[ -f $stage/output/runsc.log ]]; then
  if ! sudo install -m 0644 "$stage/output/runsc.log" "${TEST_UNDECLARED_OUTPUTS_DIR:?}/runsc.log"; then
    if (( status == 0 )); then status=1; fi
  fi
fi
exit "$status"
