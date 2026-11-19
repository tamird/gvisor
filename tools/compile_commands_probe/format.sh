#!/usr/bin/env bash
set -euo pipefail
cd "$BUILD_WORKSPACE_DIRECTORY"
exec bash tools/lint.sh --bazel="$QUALIFICATION_BAZEL" buildifier
