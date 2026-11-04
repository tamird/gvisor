#!/bin/bash
set -euo pipefail
exec python3 "$TEST_SRCDIR/$TEST_WORKSPACE/test/rbe/python_release_isolation/compare.py"
