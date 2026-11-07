#!/bin/bash
set -euo pipefail
export RUNNER_TEMP="$TEST_TMPDIR"
exec python3 -B "$TEST_SRCDIR/$TEST_WORKSPACE/test/rbe/cross_release_guest.py" guest
