#!/bin/bash
set -euo pipefail
exec timeout --signal=TERM --kill-after=120s 12m bash "$(dirname "${BASH_SOURCE[0]}")/lint.sh" source
