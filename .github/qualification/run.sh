#!/bin/bash
set -euo pipefail
printf "%s\n" "intentional failure" > "$RESULT_DIR/negative-smoke.txt"
exit 23
