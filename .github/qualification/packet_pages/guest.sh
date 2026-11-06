#!/bin/bash
set -euo pipefail
test "$(getconf PAGESIZE)" = 65536
test "$(uname -m)" = aarch64
cd "$TEST_SRCDIR/$TEST_WORKSPACE/test/rbe/packet_pages"
if unshare --net -- ./stock -test.v -test.run='^TestPacketMMapDeliversUDP$' >"$TEST_TMPDIR/stock.log" 2>&1; then
  echo 'stock unexpectedly accepted the 64 KiB packet ring'
  exit 1
fi
grep -oF 'tpBlockSize: 2101248 is not page aligned, pagesize: 65536' "$TEST_TMPDIR/stock.log"
if ! unshare --net -- ./fixed -test.v -test.run='^TestPacketMMapDeliversUDP$' >"$TEST_TMPDIR/fixed.log" 2>&1; then
  tail -c 1800 "$TEST_TMPDIR/fixed.log"
  exit 1
fi
grep -F 'PACKET_RING setup arch=arm64 page=65536 bytes=2162688' "$TEST_TMPDIR/fixed.log"
grep -F 'PACKET_RING delivered 40 sequential UDP markers' "$TEST_TMPDIR/fixed.log"
grep -F -- '--- PASS: TestPacketMMapDeliversUDP' "$TEST_TMPDIR/fixed.log"
