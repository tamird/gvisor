#!/bin/bash
set -euo pipefail
root="$TEST_SRCDIR/$TEST_WORKSPACE"
out="$TEST_UNDECLARED_OUTPUTS_DIR"
stage=/work/permanent
mkdir -p "$stage" "$out"
tar -xf "$root/test/rbe/tcg/vfio_suite.tar" -C "$stage"
cd "$stage"
sha256sum -c sha256 > "$out/identity.txt"
# The initramfs is BusyBox; supply the exact runtime loader/libc used by the
# normal syscall suite on both Linux and gVisor, not the host system's files.
tar -xf runtime-libs.tar -C /
export RUNTIME="$stage/release/runsc"
export GVISOR_SIDECAR_BINARIES_DIR="$stage/release/gvisor-bin"
status=0
for platform in native runsc_systrap; do
  exports="/work/$platform-outputs"
  mkdir -p "$exports" "/work/tmp/$platform"
  rc=0
  TEST_TMPDIR="/work/tmp/$platform" TEST_UNDECLARED_OUTPUTS_DIR="$exports" XML_OUTPUT_FILE="$exports/test.xml" "./$platform" --add-host-vfio --setup-container="$stage/test/runner/setup_container/setup_container" > "$out/$platform.txt" 2>&1 || rc=$?
  echo "$platform: $rc" >> "$out/verdict.txt"
  cat "$out/$platform.txt"
  if [[ $platform = runsc_systrap ]]; then
    find "$exports" -type f \( -name "*.txt" -o -name "*.log" -o -name "*.txt.*" \) -exec grep -hEi 'failed|warning|error|vfio|denied|permission' {} + 2>/dev/null | tail -70 > "$out/runtime-diagnostics.txt" || :
  fi
  if (( rc != 0 )) || ! grep -q 'OK .*VfioPCIDeviceTest.Readiness' "$out/$platform.txt"; then status=1; fi
done
exit "$status"
