#!/bin/bash
set -euo pipefail
stage="$1"
w="$RUNNER_TEMP/state-working-set"
owned=$(python3 -B "$w/measure.py" host "$stage")
exec unshare --mount --cgroup --propagation private sh -ec '
    mount --bind "$3" /sys/fs/cgroup
    exec /usr/bin/python3 -B "$1/measure.py" "$2"
' sh "$w" "$stage" "$owned"
