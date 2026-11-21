#!/bin/bash
set -euo pipefail
fixture=$(mktemp -d "$RUNNER_TEMP/declared-formatting-preflight.XXXXXX")
trap 'rm -rf "$fixture"' EXIT
printf 'package main\n\nfunc main() {}\n' > "$fixture/expected"
printf 'package main;func main(){}\n' > "$fixture/literal name.go"
status=0
bazel run --announce_rc=false --lockfile_mode=off --config="$QUAL_CONFIG" \
  //tools/lint:format_preflight -- "$fixture/literal name.go" \
  > "$RESULT_DIR/literal-fix.log" 2>&1 || status=$?
cat "$RESULT_DIR/literal-fix.log"
printf 'literal_fix_exit_status=%s\n' "$status" | tee "$RESULT_DIR/literal-result.txt"
if [[ "$status" -ne 0 ]]; then
  exit "$status"
fi
cmp "$fixture/expected" "$fixture/literal name.go"
status=0
bazel run --announce_rc=false --lockfile_mode=off --config="$QUAL_CONFIG" \
  //tools/lint:format_preflight -- "$fixture/missing.go" \
  > "$RESULT_DIR/missing-fix.log" 2>&1 || status=$?
cat "$RESULT_DIR/missing-fix.log"
printf 'missing_fix_exit_status=%s\n' "$status" | tee "$RESULT_DIR/missing-result.txt"
test "$status" -ne 0
