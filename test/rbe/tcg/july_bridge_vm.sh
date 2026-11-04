#!/bin/bash
# Run the original JULY guest application with neither host NICs nor shared filesystems.
set -euo pipefail
arm=$1
base=$TEST_SRCDIR/$TEST_WORKSPACE/test/rbe/tcg
host=$(realpath "$base/kvm_host_tools")
disk=$(realpath "$base/july_bridge_inputs.ext4")
kernel=$(realpath "$base/amd64_cgroup_v1_guest.Image")
rootfs=$(realpath "$base/amd64_cgroup_v1_guest.ext4")
[[ $EUID != 0 && ! -w /var/run/docker.sock ]]
[[ $arm == stock || $arm == fixed ]]
test "$(awk '/^NoNewPrivs:/{print $2}' /proc/self/status)" = 1
test "$(awk '/^CapEff:/{print $2}' /proc/self/status)" = 0000000000000000
export TMPDIR="$TEST_TMPDIR" QEMU_MODULE_DIR="$host/usr/lib/x86_64-linux-gnu/qemu"
loader="$host/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
lib="$host/lib/x86_64-linux-gnu:$host/usr/lib/x86_64-linux-gnu"
machine=(-cpu max -accel tcg,thread=multi)
backend=tcg
if [[ -c /dev/kvm && -r /dev/kvm && -w /dev/kvm ]]; then
  machine=(-cpu host -accel kvm)
  backend=kvm
fi
echo "ECHO_VM_BACKEND $backend"
log="$TEST_TMPDIR/serial"
rc=0
(
  ulimit -f 32768
  timeout -k 8 790 "$loader" --inhibit-cache --library-path "$lib" "$host/usr/bin/qemu-system-x86_64" \
    -no-user-config -nodefaults -display none -monitor none -machine pc-i440fx-6.2 "${machine[@]}" \
    -smp 2 -m 4096 -nic none -L "$host/usr/share/qemu" -bios "$host/usr/share/seabios/bios-256k.bin" \
    -kernel "$kernel" -append "console=ttyS0 root=/dev/vdb rootfstype=ext4 rw rootwait init=/init panic=-1 gvisor_echo=$arm" \
    -serial "file:$log" -drive "file=$disk,format=raw,if=none,id=input,readonly=on" \
    -device virtio-blk-pci,drive=input,addr=4 -drive "file=$rootfs,format=raw,if=none,id=root,snapshot=on" \
    -device virtio-blk-pci,drive=root,addr=5
) >"$TEST_TMPDIR/qemu.log" 2>&1 || rc=$?
echo "ECHO_VM_EXIT $rc"
# Emit fixed fixture records only. Diagnostics not matching these types remain local.
if [[ -f $log ]]; then
  tr -d '\r' <"$log" | grep -E '^(ECHO_VM_(END|READY|ELF|RUNTIME|STAGE) |DIND_(STEP|MTU|FORWARD|END|DAEMON|NETWORK|SERVER|SNAT|CT|ROUTE) |CELL |TALLY |FIRST )' || :
fi
if (( rc )); then
  grep -Eo 'Permission denied|No space left on device|could not find PC BIOS|Could not access KVM kernel module|Could not initialize SDL|unable to find CPU model|invalid accelerator|Could not open|failed to initialize kvm' "$TEST_TMPDIR/qemu.log" | head -4 || :
  exit "$rc"
fi
grep -Fxq "ECHO_VM_END $arm 0" <(tr -d '\r' <"$log")
