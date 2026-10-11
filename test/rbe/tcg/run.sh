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

# The selected machine determines whether the host needs KVM device access.
set -euo pipefail
host_archive="$(realpath "$1")"
kernel="$(realpath "$2")"
initramfs="$(realpath "$3")"
payload_archive="$(realpath "$4")"
payload="$5"
tar_tool="$(realpath "$6")"
payload_label="$7"
machine="$8"
test_setup="$(realpath "$9")"
shift 9
case "$machine" in
  arm64_tcg)
    emulator=qemu-system-aarch64
    machine_options=(-machine "virt-6.2,gic-version=3" -cpu cortex-a57 -accel "tcg,thread=multi")
    console=ttyAMA0
    ;;
  amd64_kvm)
    [[ -c /dev/kvm && -r /dev/kvm && -w /dev/kvm ]]
    emulator=qemu-system-x86_64
    # The RC guest must retain VMX/SVM so the payload can run gVisor's KVM
    # platform. Never fall back to emulation if this host cannot provide it.
    # https://docs.kernel.org/virt/kvm/x86/running-nested-guests.html
    machine_options=(-machine pc-i440fx-6.2 -cpu host -accel kvm)
    console=ttyS0
    ;;
  *) printf 'Unsupported guest machine: %s\n' "$machine" >&2; exit 1 ;;
esac
scratch="$(mktemp -d "${TEST_TMPDIR}/tcg.XXXXXX")"
result="${TEST_UNDECLARED_OUTPUTS_DIR}/guest"
mkdir -p "$scratch/host" "$scratch/input" "$result"
"$tar_tool" -xf "$host_archive" -C "$scratch/host" --no-same-owner
host="$scratch/host"
if [[ "$machine" == amd64_kvm ]]; then
  # Ubuntu's use-fixed-data-path.patch makes firmware defaults absolute.
  # Select the declared BIOS instead of the host's /usr/share/seabios.
  # https://snapshot.ubuntu.com/ubuntu/20260928T000000Z/pool/main/q/qemu/qemu_6.2+dfsg-2ubuntu6.31.debian.tar.xz
  [[ -r "$host/usr/share/seabios/bios-256k.bin" ]]
  machine_options+=(-bios "$host/usr/share/seabios/bios-256k.bin")
fi
loader="$host/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
libraries="$host/lib/x86_64-linux-gnu:$host/usr/lib/x86_64-linux-gnu"
host_tool() { "$loader" --inhibit-cache --library-path "$libraries" "$host/$1" "${@:2}"; }
# A private copy prevents the guest from modifying a Bazel input. The export
# itself is read-only as well; the writable export contains only test outputs.
cp "$payload_archive" "$scratch/input/payload.tar"
cp "$test_setup" "$scratch/input/test-setup.sh"
host_tool usr/bin/truncate -s 4G "$scratch/scratch.ext4"
MKE2FS_CONFIG="$host/etc/mke2fs.conf" host_tool sbin/mke2fs -q -t ext4 -F -m 0 "$scratch/scratch.ext4"
{
  printf '#!/bin/bash\nset -uo pipefail\n'
  printf 'export TEST_SRCDIR=%q\n' "/work/payload/$payload.runfiles"
  printf 'export TEST_WORKSPACE=%q\n' "$TEST_WORKSPACE"
  printf 'export TEST_TARGET=%q\n' "$payload_label"
  printf 'export TEST_BINARY=%q\n' "$payload"
  printf 'export TEST_TMPDIR=/work/tmp\nexport TEST_UNDECLARED_OUTPUTS_DIR=/work/outputs\nexport XML_OUTPUT_FILE=/work/test.xml\n'
  # Keep the canonical harness's outputs on ext4 until the payload has exited.
  for variable in TEST_PREMATURE_EXIT_FILE TEST_WARNINGS_OUTPUT_FILE TEST_LOGSPLITTER_OUTPUT_FILE TEST_INFRASTRUCTURE_FAILURE_FILE TEST_UNUSED_RUNFILES_LOG_FILE TEST_UNDECLARED_OUTPUTS_MANIFEST TEST_UNDECLARED_OUTPUTS_ANNOTATIONS TEST_UNDECLARED_OUTPUTS_ANNOTATIONS_DIR; do
    printf 'export %s=/work/harness/%s\n' "$variable" "$variable"
  done
  for variable in TEST_SHARD_INDEX TEST_TOTAL_SHARDS TEST_FILTER TEST_TIMEOUT TEST_SIZE TEST_RANDOM_SEED TESTBRIDGE_TEST_ONLY GVISOR_TEST_CLOCK_SOURCE; do
    if [[ -v "$variable" ]]; then printf 'export %s=%q\n' "$variable" "${!variable}"; fi
  done
  if [[ -n "${TEST_SHARD_STATUS_FILE:-}" ]]; then
    printf 'export TEST_SHARD_STATUS_FILE=/work/shard_status\n'
  fi
  # The guest harness owns aliases, runfiles lookup and fallback XML. The outer
  # harness packages returned outputs, so the guest does not request a ZIP.
  printf 'export RUNFILES_DIR="$TEST_SRCDIR"\nexport EXPERIMENTAL_SPLIT_XML_GENERATION=0\ncd /work/payload\n'
  printf 'exec /bin/bash /input/test-setup.sh %q' "$payload"
  if (( $# )); then printf ' %q' "$@"; fi
  printf '\n'
} > "$scratch/input/launch.sh"
qemu_pid=""
cleanup() {
  local status=$?
  # The executor and test harness may both signal the launcher. Keep a later
  # TERM or INT from interrupting recovery; the executor's KILL is authoritative.
  trap '' TERM INT
  trap - EXIT
  set +e
  if [[ -n "$qemu_pid" ]]; then
    # Reap the writer before inspecting its disk, even if it is unresponsive.
    kill -KILL "$qemu_pid" 2>/dev/null || true
    wait "$qemu_pid" 2>/dev/null || true
  fi
  if [[ "$status" != 0 && ! -f "$result/exit_status" ]]; then
    local recovery="${TEST_UNDECLARED_OUTPUTS_DIR}/guest-recovery"
    mkdir -p "$recovery"
    printf '%s\n' \
      "Original host exit status: $status" \
      'Incomplete guest disk recovery after bounded journal replay; no completion claim.' \
      'Guest memory and uncommitted filesystem writes may be absent.' \
      > "$recovery/README.txt"
    # Replay committed metadata on this stopped, disposable disk before reading
    # its allocation bitmaps. journal_only excludes a full filesystem check;
    # -p makes journal recovery noninteractive. Retain failures as diagnostics.
    E2FSCK_CONFIG=/dev/null host_tool usr/bin/timeout --signal=KILL 5 \
      "$loader" --inhibit-cache --library-path "$libraries" \
      "$host/sbin/e2fsck" -p -E journal_only "$scratch/scratch.ext4" \
      </dev/null > "$recovery/journal.log" 2>&1
    printf '%s\n' "$?" > "$recovery/journal_exit_status"
    # The existing e2fsprogs input supplies debugfs. Without -w it only reads
    # the stopped guest disk. Keep partial files and diagnostics if extraction
    # fails or exhausts this bounded cleanup window; never promote recovered
    # XML or shard status into a completed outer test result.
    (
      cd "$recovery" || exit
      host_tool usr/bin/timeout --signal=KILL 5 \
        "$loader" --inhibit-cache --library-path "$libraries" \
        "$host/sbin/debugfs" -R 'rdump /outputs /harness /test.xml /shard_status .' \
        "$scratch/scratch.ext4"
    ) > "$recovery/debugfs.log" 2>&1
    printf '%s\n' "$?" > "$recovery/debugfs_exit_status"
  fi
  rm -rf "$scratch"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
# Networking and privileged host mounts are not needed by the guest transport.
QEMU_MODULE_DIR="$host/usr/lib/x86_64-linux-gnu/qemu" \
  "$loader" --inhibit-cache --library-path "$libraries" "$host/usr/bin/$emulator" \
  -no-user-config -nodefaults -display none -monitor none \
  "${machine_options[@]}" \
  -smp 2 -m 3072 -nic none -L "$host/usr/share/qemu" \
  -kernel "$kernel" -initrd "$initramfs" \
  -append "console=$console rdinit=/init panic=-1" \
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
