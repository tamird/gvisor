#!/bin/bash
# Root review required before dispatch; no project build or source edits.
set -euo pipefail
if test "${VFIO_FUSE_PROBE_BOUND:-}" != 1;then
 exec timeout --signal=TERM --kill-after=60s 9m env VFIO_FUSE_PROBE_BOUND=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}"
test "$SOURCE" = 1b37f9cfca65287f8c153314e392d4fbf7c4094b
test "$BASE" = 1f33fe3003455db178b97f3b43d3a012f1611994
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
support=$(cd "$(dirname "$0")" && pwd)
w="$RUNNER_TEMP/vfio-fuse-prerequisite"
r="$RESULT_DIR/vfio-fuse-prerequisite"
mkdir -m 700 "$w" "$r"
cleanup() {
 local rc=$?
 trap - EXIT
 trap '' TERM INT
 timeout --kill-after=5s 35s python3 "$support/probe.py" cleanup "$w" "$r" "$support" || rc=1
 timeout --kill-after=1s 5s git diff --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --quiet || rc=1
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
timeout --kill-after=5s 7m python3 "$support/probe.py" run "$w" "$r" "$support"
