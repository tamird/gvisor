#!/bin/bash
set -euo pipefail
data="$TEST_SRCDIR/$TEST_WORKSPACE/test/rbe/tcg/vfio_pci_root.tar"
runtime="$TEST_TMPDIR/native-runtime"
mkdir -p "$runtime"
tar -xf "$data" -C "$runtime"
interpreter=$(cat "$runtime/interpreter")
libs="$runtime/lib/x86_64-linux-gnu:$runtime/usr/lib/x86_64-linux-gnu:$runtime/lib64"
native=("$runtime$interpreter" --library-path "$libs" "$runtime/probe")
exec "${native[@]}" --gtest_filter=VfioPCIDeviceTest.Readiness
