#!/bin/bash
set -euo pipefail
scratch=$(mktemp -d "$RUNNER_TEMP/cc-format-scope.XXXXXX")
cleanup() {
  if [[ -e "$scratch/git" ]]; then mv "$scratch/git" "$PWD/.git"; fi
  rm -rf "$scratch"
}
trap cleanup EXIT
export LINT_CACHE_DIR="$scratch/cache"
query_options=(--announce_rc=false --lockfile_mode=off --config="$QUAL_CONFIG")
bazel cquery "${query_options[@]}" --output=starlark \
  '--starlark:expr=json.encode([f.short_path for f in target.files.to_list()])' \
  @lint_inputs//:cc > "$RESULT_DIR/full-inputs.json"
git ls-files -z -- '*.c' '*.cc' '*.h' > "$scratch/indexed-inputs"
python3 - "$scratch/indexed-inputs" <<'EOF'
import json, os, sys
from pathlib import Path
expected = set(Path(sys.argv[1]).read_bytes().decode().split('\0')) - {''}
actual = {value.split('ROOT.d/', 1)[1] for value in json.loads(Path(os.environ['RESULT_DIR'], 'full-inputs.json').read_text())}
assert actual == expected, (actual - expected, expected - actual)
print(f'Normal index full C/C++ membership: PASS ({len(actual)} files)')
EOF
tools/lint.sh --bazel="$(command -v bazel)" \
  --bazel-options="--lockfile_mode=off --config=$QUAL_CONFIG" clang-format \
  2>&1 | tee "$RESULT_DIR/full-check.log"
mv .git "$scratch/git"
bazel cquery "${query_options[@]}" //pkg/tcpip/seqnum:seqnum \
  > "$RESULT_DIR/archive-unrelated.log" 2>&1
printf 'Archive-style checkout without .git: unrelated target analysis PASS\n'
bazel cquery "${query_options[@]}" --ignore_dev_dependency //pkg/tcpip/seqnum:seqnum \
  > "$RESULT_DIR/ignored-dev.log" 2>&1
printf 'Imported-module dev-dependency model: unrelated target analysis PASS\n'
status=0
bazel cquery "${query_options[@]}" //tools/lint:clang_format \
  > "$RESULT_DIR/archive-lint.log" 2>&1 || status=$?
cat "$RESULT_DIR/archive-lint.log"
test "$status" -ne 0
grep -F 'Source lint requires a Git checkout and Git on PATH' "$RESULT_DIR/archive-lint.log"
printf 'Archive developer lint fails explicitly without fallback: PASS\n'
mv "$scratch/git" .git
status=0
bazel run "${query_options[@]}" @buildifier_prebuilt//:buildifier -- \
  -mode=diff MODULE.bazel tools/lint/BUILD tools/lint_sources.bzl \
  > "$RESULT_DIR/buildifier.diff" 2> "$RESULT_DIR/buildifier.log" || status=$?
cat "$RESULT_DIR/buildifier.log"
cat "$RESULT_DIR/buildifier.diff"
printf 'buildifier_diff_status=%s\n' "$status"
# A formatting diff is an artifact for the immutable source owner; tool errors fail.
[[ "$status" -eq 0 || "$status" -eq 4 ]]
