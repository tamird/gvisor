#!/bin/bash
set -euo pipefail
bazel cquery --announce_rc=false --lockfile_mode=off --config="$QUAL_CONFIG" --output=jsonproto \
  //tools/lint:declared_formatting_preflight > "$RESULT_DIR/configured-targets.json"
