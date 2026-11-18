#!/bin/bash
set -euo pipefail
exec timeout --signal=TERM --kill-after=120s 42m bash "$(dirname "${BASH_SOURCE[0]}")/lint.sh" cpp
