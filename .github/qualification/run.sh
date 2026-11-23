#!/bin/bash
set -euo pipefail
{
  uname -sm
  bazel --version
  timeout 10s docker version --format '{{json .Server}}'
  timeout 10s docker info --format '{{json .Runtimes}}'
  timeout 10s docker image ls --filter 'reference=*nodejs22.2.0*' --format '{{json .}}'
} >"$RESULT_DIR/node-runtime-preflight.log" 2>&1
cat "$RESULT_DIR/node-runtime-preflight.log"
