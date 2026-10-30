#!/bin/bash
# Fork qualification only. The parent guest binds its isolated synthetic PCI NIC.
set -euo pipefail
root="$TEST_SRCDIR/$TEST_WORKSPACE"
probe="$root/test/syscalls/linux/vfio_pci_device_test"
out="$TEST_UNDECLARED_OUTPUTS_DIR"
cd "$TEST_TMPDIR"
mkdir -p "$out"
printf 'device=%s group=%s lower_driver=%s\n' "$TEST_VFIO_DEVICE" "$TEST_VFIO_GROUP" "$(readlink "/sys/bus/pci/devices/$TEST_VFIO_DEVICE/driver")" | tee "$out/source-device"
"$probe" | tee "$out/native-gtest.txt"
grep -Fq '[       OK ] VfioPCIDeviceTest.Readiness' "$out/native-gtest.txt"
grep -Fq '[       OK ] VfioPCIDeviceTest.InterruptEventfdKeepsFullCounter' "$out/native-gtest.txt"

for arm in stock fixed; do
  bundle="$PWD/$arm"
  mkdir -p "$bundle/rootfs" "$bundle/state" "$bundle/debug"
  cp "$probe" "$bundle/rootfs/probe"
  cat > "$bundle/config.json" <<JSON
{"ociVersion":"1.0.2","root":{"path":"rootfs"},"process":{"terminal":false,"user":{"uid":0,"gid":0},"args":["/probe"],"env":["PATH=/","TEST_VFIO_GROUP=$TEST_VFIO_GROUP","TEST_VFIO_DEVICE=$TEST_VFIO_DEVICE"],"cwd":"/"},"mounts":[{"type":"proc","destination":"/proc","source":"proc"},{"type":"sysfs","destination":"/sys","source":"sysfs"}],"linux":{"namespaces":[{"type":"pid"},{"type":"mount"},{"type":"ipc"},{"type":"uts"},{"type":"network"}],"devices":[{"path":"/dev/vfio/vfio","type":"c","major":$((16#$(stat -c %t /dev/vfio/vfio))),"minor":$((16#$(stat -c %T /dev/vfio/vfio))),"fileMode":438},{"path":"$TEST_VFIO_GROUP","type":"c","major":$((16#$(stat -c %t "$TEST_VFIO_GROUP"))),"minor":$((16#$(stat -c %T "$TEST_VFIO_GROUP"))),"fileMode":438}]}}
JSON
  bin="$root/test/rbe/tcg/vfio_$arm/runsc"
  sha256sum "$bin" "$(dirname "$bin")"/gvisor-bin/* > "$out/$arm-sha"
  rc=0
  "$bin" --root="$bundle/state" --platform=systrap --network=none --overlay2=none --directfs=true --host-settings=ignore --ignore-cgroups --tpuproxy --debug --debug-log="$bundle/debug/" run --bundle="$bundle" vfio > "$out/$arm-gtest.txt" 2>&1 || rc=$?
  cat "$out/$arm-gtest.txt"
  grep -Ei 'error|VFIO|failed|FATAL| panic' "$bundle"/debug/* | tail -75 > "$out/$arm-diagnostics.txt" || :
  if [[ $arm == stock ]]; then
    test "$rc" -eq 1
    grep -Fq '[  FAILED  ] VfioPCIDeviceTest.Readiness' "$out/$arm-gtest.txt"
    grep -Fq '[  FAILED  ] VfioPCIDeviceTest.InterruptEventfdKeepsFullCounter' "$out/$arm-gtest.txt"
    grep -q 'poll(&pfd' "$out/$arm-gtest.txt"
    grep -q 'count.*saved' "$out/$arm-gtest.txt"
    grep -q '4294967297' "$out/$arm-gtest.txt"
  else
    test "$rc" -eq 0
    grep -Fq '[       OK ] VfioPCIDeviceTest.Readiness' "$out/$arm-gtest.txt"
    grep -Fq '[       OK ] VfioPCIDeviceTest.InterruptEventfdKeepsFullCounter' "$out/$arm-gtest.txt"
  fi
done
