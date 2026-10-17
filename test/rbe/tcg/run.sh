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
# These timestamps share only the host's monotonic boot clock. Guest kernel
# uptime has a different origin and must not be subtracted from these values.
host_phase() {
  local uptime _ record
  # Diagnostic writes must not replace the payload or signal exit status.
  read -r uptime _ < /proc/uptime || return 0
  printf -v record 'host_uptime_seconds=%s phase=%s status=%s outer_test_timeout_seconds=%s' \
    "$uptime" "$1" "${2:--}" "${TEST_TIMEOUT:-unset}"
  printf '%s\n' "$record" >> "${TEST_UNDECLARED_OUTPUTS_DIR}/host-phases.log" || true
  # The common report bundle always retains test.log, including passing runs.
  printf '%s\n' "$record" || true
}
host_phase launcher_start
host="$(realpath "$1")"
kernel="$(realpath "$2")"
rootfs="$(realpath "$3")"
payload_archive="$(realpath "$4")"
payload="$5"
payload_label="$6"
machine="$7"
test_setup="$(realpath "$8")"
shift 8
# The owning rule writes this declared configuration beside the launcher.
declare guest_memory_mib guest_scratch_gib guest_run_under guest_emulator guest_arm64_el2
declare -a guest_env guest_inherited_env
# shellcheck disable=SC1090
source "${GVISOR_TEST_GUEST_CONFIG:?}"
if [[ -n "$guest_emulator" ]]; then guest_emulator="$(realpath "$guest_emulator")"; fi
case "$machine" in
  arm64_tcg)
    emulator=qemu-system-aarch64
    # QEMU's software CPU identity avoids Linux workarounds for physical CPU
    # errata, which can prevent the emulated guest from entering KVM.
    # https://github.com/qemu/qemu/blob/4fc49f46d/target/arm/tcg/cpu64.c#L1189-L1206
    machine_options=(-machine "virt-6.2,gic-version=3" -cpu max -accel "tcg,thread=multi")
    if [[ "$guest_arm64_el2" == true ]]; then
      machine_options[1]+=",virtualization=on"
    fi
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
mkdir -p "$scratch/input" "$result"
host_phase host_tools_ready
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
host_phase payload_copy_ready
host_tool usr/bin/truncate -s "${guest_scratch_gib}G" "$scratch/scratch.ext4"
MKE2FS_CONFIG="$host/etc/mke2fs.conf" host_tool sbin/mke2fs -q -t ext4 -F -m 0 "$scratch/scratch.ext4"
host_phase scratch_ready
{
  printf '#!/bin/bash\nset -uo pipefail\n'
  for assignment in "${guest_env[@]}"; do
    printf 'export %q\n' "$assignment"
  done
  for variable in "${guest_inherited_env[@]}"; do
    if [[ -v "$variable" ]]; then printf 'export %s=%q\n' "$variable" "${!variable}"; fi
  done
  printf 'export TEST_SRCDIR=%q\n' "/work/payload/$payload.runfiles"
  printf 'export TEST_WORKSPACE=%q\n' "$TEST_WORKSPACE"
  printf 'export TEST_TARGET=%q\n' "$payload_label"
  printf 'export TEST_BINARY=%q\n' "$payload"
  printf 'export TEST_TMPDIR=/work/tmp\nexport TEST_UNDECLARED_OUTPUTS_DIR=/work/outputs\nexport XML_OUTPUT_FILE=/work/test.xml\n'
  # Keep the canonical harness's outputs on ext4 until the payload has exited.
  for variable in TEST_PREMATURE_EXIT_FILE TEST_WARNINGS_OUTPUT_FILE TEST_LOGSPLITTER_OUTPUT_FILE TEST_INFRASTRUCTURE_FAILURE_FILE TEST_UNUSED_RUNFILES_LOG_FILE TEST_UNDECLARED_OUTPUTS_MANIFEST TEST_UNDECLARED_OUTPUTS_ANNOTATIONS TEST_UNDECLARED_OUTPUTS_ANNOTATIONS_DIR; do
    printf 'export %s=/work/harness/%s\n' "$variable" "$variable"
  done
  for variable in TEST_SHARD_INDEX TEST_TOTAL_SHARDS TEST_FILTER TEST_TIMEOUT TEST_SIZE TEST_RANDOM_SEED TESTBRIDGE_TEST_ONLY GVISOR_TEST_CLOCK_SOURCE GO_TEST_WRAP_TESTV; do
    if [[ -v "$variable" ]]; then printf 'export %s=%q\n' "$variable" "${!variable}"; fi
  done
  if [[ -n "${TEST_SHARD_STATUS_FILE:-}" ]]; then
    printf 'export TEST_SHARD_STATUS_FILE=/work/shard_status\n'
  fi
  # The guest harness owns aliases, runfiles lookup and fallback XML. The outer
  # harness packages returned outputs, so the guest does not request a ZIP.
  printf 'export RUNFILES_DIR="$TEST_SRCDIR"\nexport EXPERIMENTAL_SPLIT_XML_GENERATION=0\ncd /work/payload\n'
  if [[ "$guest_arm64_el2" == true ]]; then
    # Require an accessible device before the payload. The selected tests
    # establish KVM ioctl support; their own skips remain incomplete.
    printf '%s\n' '[[ -c /dev/kvm && -r /dev/kvm && -w /dev/kvm ]] || { echo "ARM64 guest KVM device unavailable" >&2; exit 1; }'
  fi
  printf 'exec /bin/bash /input/test-setup.sh'
  if [[ -n $guest_run_under ]]; then
    # Both executables and their runfiles are in the declared payload archive.
    # Absolute paths remain valid after the harness enters payload runfiles.
    printf ' %q' "/work/payload/$guest_run_under" "/work/payload/$payload"
  else
    printf ' %q' "$payload"
  fi
  if (( $# )); then printf ' %q' "$@"; fi
  printf '\n'
} > "$scratch/input/launch.sh"
host_phase launch_ready
qemu_pid=""
cleanup() {
  local status=$?
  # The executor and test harness may both signal the launcher. Keep a later
  # TERM or INT from interrupting recovery; the executor's KILL is authoritative.
  trap '' TERM INT
  trap - EXIT
  set +e
  host_phase cleanup_start "$status"
  if [[ -n "$qemu_pid" ]]; then
    # Reap the writer before inspecting its disk, even if it is unresponsive.
    kill -KILL "$qemu_pid" 2>/dev/null || true
    wait "$qemu_pid" 2>/dev/null || true
    host_phase cleanup_qemu_reaped
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
    host_phase recovery_complete
  fi
  host_phase scratch_remove_start
  rm -rf "$scratch"
  host_phase scratch_remove_return "$?"
  host_phase launcher_return "$status"
  exit "$status"
}
trap cleanup EXIT
trap 'host_phase signal_TERM 143; exit 143' TERM
trap 'host_phase signal_INT 130; exit 130' INT
# ARM TCG supplies its source-built emulator. Guest preparation, recovery,
# shared libraries and firmware use the immutable declared host-tool directory.
emulator_path="${guest_emulator:-$host/usr/bin/$emulator}"
# Networking and privileged host mounts are not needed by the guest transport.
# snapshot=on opens the declared root as a read-only backing file. QEMU keeps
# its temporary writable overlay in this test's scratch via TMPDIR; the raw
# scratch disk stays independently writable for existing failure recovery.
TMPDIR="$scratch" QEMU_MODULE_DIR="$host/usr/lib/x86_64-linux-gnu/qemu" \
  "$loader" --inhibit-cache --library-path "$libraries" "$emulator_path" \
  -no-user-config -nodefaults -display none -monitor none \
  "${machine_options[@]}" \
  -smp 2 -m "$guest_memory_mib" -nic none -L "$host/usr/share/qemu" \
  -kernel "$kernel" \
  -append "console=$console root=/dev/vdb rootfstype=ext4 rw rootwait init=/init panic=-1" \
  -serial "file:${TEST_UNDECLARED_OUTPUTS_DIR}/console.log" \
  -drive "file=$scratch/scratch.ext4,format=raw,if=none,id=scratch" \
  -device virtio-blk-pci,drive=scratch,addr=4 \
  -drive "file=$rootfs,format=raw,if=none,id=root,snapshot=on" \
  -device virtio-blk-pci,drive=root,addr=5 \
  -fsdev "local,id=input,path=$scratch/input,security_model=none,readonly=on" \
  -device virtio-9p-pci,fsdev=input,mount_tag=input \
  -fsdev "local,id=result,path=$result,security_model=none" \
  -device virtio-9p-pci,fsdev=result,mount_tag=result &
qemu_pid=$!
host_phase qemu_started
qemu_status=0
wait "$qemu_pid" || qemu_status=$?
qemu_pid=""
host_phase qemu_wait_return "$qemu_status"
cat "${TEST_UNDECLARED_OUTPUTS_DIR}/console.log"
host_phase console_copied
if [[ -n "${TEST_SHARD_STATUS_FILE:-}" && -f "$result/shard_status" ]]; then
  touch "$TEST_SHARD_STATUS_FILE"
fi
if [[ -f "$result/test.xml" ]]; then cp "$result/test.xml" "$XML_OUTPUT_FILE"; fi
host_phase reports_copied
[[ "$qemu_status" == 0 ]] || exit "$qemu_status"
[[ -f "$result/exit_status" ]] || { echo 'Guest did not report a completed test' >&2; exit 1; }
read -r status < "$result/exit_status"
[[ "$status" =~ ^[0-9]+$ && "$status" -le 255 ]] || exit 1
[[ "$status" != 0 || -s "$result/test.xml" ]] || { echo 'Passing guest omitted test XML' >&2; exit 1; }
host_phase guest_status_read "$status"
exit "$status"
