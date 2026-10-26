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

# The caller supplies the extracted, declared host-tool directory in host.
# Absolute loader symlinks in the package cannot be followed on the host.
host_tool() {
  "$host/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2" --inhibit-cache \
    --library-path "$host/lib/x86_64-linux-gnu:$host/usr/lib/x86_64-linux-gnu" "$host/$1" "${@:2}"
}

# Populate an ext4 image without privileged mounts. The upstream fakeroot
# wrapper supplies root numeric ownership, manages its daemon and propagates
# mke2fs's status. Only its getopt probe and optional environment reads require
# errexit/nounset off; the surrounding subshell still reports a failed mke2fs.
build_ext4() {
  local tree="$1" output="$2" size="$3" label="$4"
  shift 4
  (
    set +eu
    # Called by the sourced upstream wrapper.
    # shellcheck disable=SC2329
    getopt() { host_tool usr/bin/getopt "$@"; }
    # shellcheck disable=SC2329
    cut() { host_tool usr/bin/cut "$@"; }
    # shellcheck disable=SC2329
    faked() { host_tool usr/bin/faked-sysv "$@"; }
    export MKE2FS_CONFIG="$host/etc/mke2fs.conf"
    # shellcheck disable=SC1091
    source "$host/usr/bin/fakeroot-sysv" \
      -l "$host/usr/lib/x86_64-linux-gnu/libfakeroot/libfakeroot-sysv.so" \
      -f faked -- host_tool sbin/mke2fs -q -t ext4 -F -m 0 \
      -d "$tree" -E root_owner=0:0 -L "$label" "$@" "$output" "$size"
  )
  E2FSCK_CONFIG=/dev/null host_tool sbin/e2fsck -fn "$output"
}
