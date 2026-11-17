#!/bin/bash
set -euo pipefail
: "${SOURCE:?}" "${BASE?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}"
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$(git show -s --format=%P HEAD | cut -d' ' -f1)" = "$BASE"
case "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" in
  amd64:x86_64:x86_64|arm64:aarch64:aarch64) ;;
  *) echo 'Architecture/config/runner mismatch' >&2; exit 1 ;;
esac
case "${QUAL_TOOLING_SCOPE:?}" in
  bazel-9.3.1) test "$(bazel --version)" = 'bazel 9.3.1' ;;
  static-tracker) test -f app.js; test -f metadata-preflight.mjs; test -f registry.json ;;
  *) echo 'Unexpected tooling scope' >&2; exit 1 ;;
esac
printf 'Dispatcher smoke passed for source=%s base=%s arch=%s config=%s\n' "$SOURCE" "$BASE" "$QUAL_ARCH" "$QUAL_CONFIG" > "$RESULT_DIR/smoke.txt"
