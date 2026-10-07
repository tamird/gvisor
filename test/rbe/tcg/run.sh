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

# Unprivileged host process; only the emulated guest has kernel privileges.
set -euo pipefail
host_archive="$(realpath "$1")"
kernel="$(realpath "$2")"
initramfs="$(realpath "$3")"
payload_archive="$(realpath "$4")"
payload="$5"
tar_tool="$(realpath "$6")"
payload_label="$7"
shift 7
scratch="$(mktemp -d "${TEST_TMPDIR}/tcg.XXXXXX")"
result="${TEST_UNDECLARED_OUTPUTS_DIR}/guest"
mkdir -p "$scratch/host" "$scratch/input" "$result"
"$tar_tool" -xf "$host_archive" -C "$scratch/host" --no-same-owner
host="$scratch/host"
loader="$host/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
libraries="$host/lib/x86_64-linux-gnu:$host/usr/lib/x86_64-linux-gnu"
host_tool() { "$loader" --inhibit-cache --library-path "$libraries" "$host/$1" "${@:2}"; }
# A private copy prevents the guest from modifying a Bazel input. The export
# itself is read-only as well; the writable export contains only test outputs.
cp "$payload_archive" "$scratch/input/payload.tar"
host_tool usr/bin/truncate -s 4G "$scratch/scratch.ext4"
MKE2FS_CONFIG="$host/etc/mke2fs.conf" host_tool sbin/mke2fs -q -t ext4 -F -m 0 "$scratch/scratch.ext4"
{
  printf '#!/bin/bash\nset -uo pipefail\n'
  printf 'export TEST_SRCDIR=%q\n' "/work/payload/$payload.runfiles"
  printf 'export TEST_WORKSPACE=%q\n' "$TEST_WORKSPACE"
  printf 'export TEST_TARGET=%q\n' "$payload_label"
  printf 'export TEST_TMPDIR=/work/tmp\nexport TEST_UNDECLARED_OUTPUTS_DIR=/work/outputs\nexport XML_OUTPUT_FILE=/work/test.xml\n'
  for variable in TEST_SHARD_INDEX TEST_TOTAL_SHARDS TEST_FILTER TEST_TIMEOUT TEST_SIZE TEST_RANDOM_SEED TESTBRIDGE_TEST_ONLY; do
    if [[ -v "$variable" ]]; then printf 'export %s=%q\n' "$variable" "${!variable}"; fi
  done
  if [[ -n "${TEST_SHARD_STATUS_FILE:-}" ]]; then
    printf 'export TEST_SHARD_STATUS_FILE=/result/shard_status\n'
  fi
  printf 'export RUNFILES_DIR="$TEST_SRCDIR"\nunset RUNFILES_MANIFEST_FILE\ncd "$TEST_SRCDIR/$TEST_WORKSPACE"\n'
  printf 'exec %q' "./$payload"
  if (( $# )); then printf ' %q' "$@"; fi
  printf '\n'
} > "$scratch/input/launch.sh"
qemu_pid=""
cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n "$qemu_pid" ]]; then
    kill "$qemu_pid" 2>/dev/null || true
    wait "$qemu_pid" 2>/dev/null || true
  fi
  rm -rf "$scratch"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
# No host devices, networking, acceleration fallback or privileged mounts.
QEMU_MODULE_DIR="$host/usr/lib/x86_64-linux-gnu/qemu" \
  "$loader" --inhibit-cache --library-path "$libraries" "$host/usr/bin/qemu-system-aarch64" \
  -no-user-config -nodefaults -display none -monitor none \
  -machine virt-6.2,gic-version=3 -cpu cortex-a57 -accel tcg,thread=multi \
  -smp 2 -m 3072 -nic none -L "$host/usr/share/qemu" \
  -kernel "$kernel" -initrd "$initramfs" \
  -append 'console=ttyAMA0 rdinit=/init panic=-1' \
  -serial "file:${TEST_UNDECLARED_OUTPUTS_DIR}/console.log" \
  -drive "file=$scratch/scratch.ext4,format=raw,if=none,id=scratch" \
  -device virtio-blk-pci,drive=scratch \
  -fsdev "local,id=input,path=$scratch/input,security_model=none,readonly=on" \
  -device virtio-9p-pci,fsdev=input,mount_tag=input \
  -fsdev "local,id=result,path=$result,security_model=none" \
  -device virtio-9p-pci,fsdev=result,mount_tag=result &
qemu_pid=$!
qemu_status=0
wait "$qemu_pid" || qemu_status=$?
qemu_pid=""
cat "${TEST_UNDECLARED_OUTPUTS_DIR}/console.log"
if [[ -n "${TEST_SHARD_STATUS_FILE:-}" && -f "$result/shard_status" ]]; then
  touch "$TEST_SHARD_STATUS_FILE"
fi
if [[ -f "$result/test.xml" ]]; then cp "$result/test.xml" "$XML_OUTPUT_FILE"; fi
[[ "$qemu_status" == 0 ]] || exit "$qemu_status"
[[ -f "$result/exit_status" ]] || { echo 'Guest did not report a completed test' >&2; exit 1; }
read -r status < "$result/exit_status"
[[ "$status" =~ ^[0-9]+$ && "$status" -le 255 ]] || exit 1
[[ "$status" != 0 || -s "$result/test.xml" ]] || { echo 'Passing guest omitted test XML' >&2; exit 1; }
exit "$status"
