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

# Build inputs are APT data archives; no package maintainer scripts run.
set -euo pipefail

tar_tool="$(realpath "$1")"
host_archive="$(realpath "$2")"
guest_archive="$(realpath "$3")"
init="$(realpath "$4")"
release="$5"
kernel_out="$(realpath -m "$6")"
initramfs_out="$(realpath -m "$7")"
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
mkdir "$staging/host" "$staging/root"
"$tar_tool" -xf "$host_archive" -C "$staging/host" --no-same-owner
"$tar_tool" -xf "$guest_archive" -C "$staging/root" --no-same-owner
host="$staging/host"
root="$staging/root"
# Match the existing declared EROFS tools' loader contract. Absolute loader
# symlinks inside a Debian data archive cannot be followed on the host.
host_tool() {
  "$host/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2" --inhibit-cache \
    --library-path "$host/lib/x86_64-linux-gnu:$host/usr/lib/x86_64-linux-gnu" "$host/$1" "${@:2}"
}
kmod_tool() {
  "$host/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2" --inhibit-cache --argv0 "$1" \
    --library-path "$host/lib/x86_64-linux-gnu:$host/usr/lib/x86_64-linux-gnu" "$host/bin/kmod" "${@:2}"
}
cp "$root/boot/vmlinuz-$release" "$kernel_out"
# Retain exactly the module closure used by this board and output transport.
# virtio-blk, virtio-pci, ext4 and devtmpfs are built into the pinned kernel.
kmod_tool depmod -b "$root" "$release"
mkdir "$staging/modules"
for module in 9p 9pnet_virtio overlay; do
  kmod_tool modprobe -C /dev/null -d "$root" -S "$release" --show-depends "$module"
done > "$staging/modules.txt"
while read -r operation path remainder; do
  [[ "$operation" == builtin ]] && continue
  [[ "$operation" == insmod && "$path" == "$root/lib/modules/$release/"* && -z "$remainder" ]]
  relative="${path#"$root/lib/modules/$release/"}"
  mkdir -p "$staging/modules/$(dirname "$relative")"
  cp "$path" "$staging/modules/$relative"
done < "$staging/modules.txt"
rm -rf "$root/lib/modules/$release/kernel"
cp -a "$staging/modules/." "$root/lib/modules/$release/"
kmod_tool depmod -b "$root" "$release"
rm -rf "${root:?}/boot"
cp "$init" "$root/init"
chmod 0755 "$root/init"
mkdir -p "$root/proc" "$root/sys" "$root/dev" "$root/run" "$root/tmp" "$root/input" "$root/result" "$root/work"
chmod 1777 "$root/tmp"
# APT archives do not run update-alternatives.
ln -sfn bash "$root/bin/sh"
ln -sfn nc.openbsd "$root/bin/nc"
# cpio owns numeric IDs, independent of the remote action's uid.
cd "$root"
host_tool usr/bin/find . -print0 | host_tool bin/cpio --null -o --format=newc --owner=0:0 --reproducible | host_tool bin/gzip -n > "$initramfs_out"
