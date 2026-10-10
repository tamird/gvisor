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
page_size="$8"
zstd="$(realpath "$9")"
architecture="${10}"
shift 10
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
mkdir "$staging/host" "$staging/root"
"$tar_tool" -xf "$host_archive" -C "$staging/host" --no-same-owner
"$tar_tool" -xf "$guest_archive" -C "$staging/root" --no-same-owner
for archive in "$@"; do
  "$tar_tool" -xf "$archive" -C "$staging/root" --no-same-owner
done
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
kernel="$root/boot/vmlinuz-$release"
# zboot stores the compressed payload inside .text, not in a separate PE
# section. Its public header supplies the offset, size and compression type:
# https://github.com/torvalds/linux/blob/fd73f4a66/drivers/firmware/efi/libstub/zboot-header.S#L19-L27
read -r dos image_type < <(host_tool usr/bin/od -An -tx4 -N8 "$kernel")
if [[ "$architecture" == amd64 ]]; then
  # x86 boot protocol: HdrS signature and the XLF_KERNEL_64 flag.
  # https://docs.kernel.org/arch/x86/boot.html
  read -r magic < <(host_tool usr/bin/od -An -tx4 -j514 -N4 "$kernel")
  read -r flags < <(host_tool usr/bin/od -An -tu2 -j566 -N2 "$kernel")
  [[ "$magic" == 53726448 && "$((flags & 1))" == 1 && "$page_size" == 4096 ]]
  cp "$kernel" "$kernel_out"
elif [[ "$dos" == 00005a4d && "$image_type" == 676d697a ]]; then
  read -r compression < <(host_tool usr/bin/od -An -tx1 -j24 -N5 "$kernel")
  [[ "$compression" == "7a 73 74 64 00" ]]
  read -r offset size < <(host_tool usr/bin/od -An -tu4 -j8 -N8 "$kernel")
  [[ "$offset" -ge 64 && "$size" -gt 0 && "$((offset + size))" -le "$(host_tool usr/bin/stat -c %s "$kernel")" ]]
  host_tool bin/dd if="$kernel" bs=1M iflag=skip_bytes,count_bytes skip="$offset" count="$size" status=none | "$zstd" -dc > "$kernel_out"
  read -r magic < <(host_tool usr/bin/od -An -tx4 -j56 -N4 "$kernel_out")
  [[ "$magic" == 644d5241 ]]
  read -r image_size flags < <(host_tool usr/bin/od -An -tu8 -j16 -N16 "$kernel_out")
  [[ "$image_size" -ge 64 && "$image_size" -le "$(host_tool usr/bin/stat -c %s "$kernel_out")" ]]
  [[ "$page_size" == "$((1 << (10 + 2 * ((flags >> 1) & 3))))" ]]
else
  # QEMU accepts both the ordinary ARM64 Image and its gzip representation.
  cp "$kernel" "$kernel_out"
fi
# Newer Ubuntu module packages use /usr/lib; Jammy kmod uses /lib/modules.
if [[ -d "$root/usr/lib/modules/$release" && ! -e "$root/lib/modules/$release" ]]; then
  mkdir -p "$root/lib/modules"
  mv "$root/usr/lib/modules/$release" "$root/lib/modules/"
fi
# Native syscall tests exercise drivers beyond those needed to boot the guest.
# Keep the package's complete module tree so kernel module autoloading works.
kmod_tool depmod -b "$root" "$release"
rm -rf "${root:?}/boot"
printf 'readonly expected_kernel_release=%q\nreadonly expected_page_size=%q\n' "$release" "$page_size" > "$root/etc/gvisor-test-kernel"
printf 'readonly expected_architecture=%q\n' "$architecture" >> "$root/etc/gvisor-test-kernel"
cp "$init" "$root/init"
chmod 0755 "$root/init"
mkdir -p "$root/proc" "$root/sys" "$root/dev" "$root/run" "$root/tmp" "$root/input" "$root/result" "$root/work"
chmod 1777 "$root/tmp"
# base-passwd normally installs these databases from its maintainer script.
# Copy its declared defaults because image construction only extracts archives.
cp "$root/usr/share/base-passwd/passwd.master" "$root/etc/passwd"
cp "$root/usr/share/base-passwd/group.master" "$root/etc/group"
# APT archives do not run update-alternatives.
ln -sfn bash "$root/bin/sh"
ln -sfn nc.openbsd "$root/bin/nc"
ln -sfn which.debianutils "$root/usr/bin/which"
# cpio owns numeric IDs, independent of the remote action's uid.
cd "$root"
host_tool usr/bin/find . -print0 | host_tool bin/cpio --null -o --format=newc --owner=0:0 --reproducible | host_tool bin/gzip -n > "$initramfs_out"
