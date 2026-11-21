#!/bin/bash
set -euo pipefail
bazel run --announce_rc=false --lockfile_mode=off --config="$QUAL_CONFIG" \
  @buildifier_prebuilt//:buildifier -- --mode=check --lint=warn \
  --warnings=-duplicated-name,-list-append,-native-py,-native-sh-binary,-native-sh-library,-native-sh-test,-native-proto \
  MODULE.bazel tools/BUILD tools/lint/BUILD tools/lint_sources.bzl
