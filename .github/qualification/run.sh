#!/bin/bash
set -euo pipefail
unset GIT_INDEX_FILE
scratch=$(mktemp -d "$RUNNER_TEMP/cc-format-all-lint.XXXXXX")
trap 'rm -rf "$scratch"' EXIT
export LINT_CACHE_DIR="$scratch/cache"
tools/lint.sh --bazel="$(command -v bazel)" \
  --bazel-options="--lockfile_mode=off --config=$QUAL_CONFIG" \
  2>&1 | tee "$RESULT_DIR/all-lint.log"
