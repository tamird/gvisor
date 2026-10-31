#!/bin/bash
set -eu
if [[ "$1" == pass ]]; then
  echo 'PASSED host-testlogs-fixture pass'
  exit 0
fi
if [[ "$1" == flaky && -f "$HOST_LOG_RETRY_MARKER" ]]; then
  echo 'PASSED host-testlogs-fixture retry'
  exit 0
fi
if [[ "$1" == flaky ]]; then
  : > "$HOST_LOG_RETRY_MARKER"
fi
echo "--- FAIL: TestHostLog_$1 (0.00s)"
echo "[  FAILED  ] HostLog.$1 (0 ms)"
echo "actual $1 boot log" > "$TEST_UNDECLARED_OUTPUTS_DIR/fixture-$1.boot.txt"
echo "actual $1 gofer log" > "$TEST_UNDECLARED_OUTPUTS_DIR/fixture-$1.gofer.txt"
exit 1
