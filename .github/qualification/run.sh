#!/bin/bash
set -euo pipefail
scratch=$(mktemp -d "$RUNNER_TEMP/cc-format-preflight.XXXXXX")
fixture="$PWD/tools/lint/preflight"
test ! -e "$fixture"
test ! -L "$fixture"
shared=""
cleanup() {
  rm -rf "$fixture" "$scratch"
  if [[ -n "$shared" ]]; then rm -f "$shared"; fi
}
trap cleanup EXIT
mkdir "$fixture"
export GIT_INDEX_FILE="$scratch/index"
export LINT_CACHE_DIR="$scratch/cache"
git read-tree --empty
printf 'BasedOnStyle: Google\nIndentWidth: 7\n' > "$fixture/.clang-format"
printf 'ignored.cc\n' > "$fixture/.clang-format-ignore"
printf 'int main() {\nint value=0;\nreturn value;\n}\n' > "$fixture/first.cc"
printf 'int ignored( ){return 0;}\n' > "$fixture/ignored.cc"
cp "$fixture/ignored.cc" "$scratch/ignored-original"
git add -- .clang-format tools/lint/preflight/first.cc tools/lint/preflight/ignored.cc tools/lint/preflight/.clang-format tools/lint/preflight/.clang-format-ignore
git update-index --split-index
shared=$(git rev-parse --path-format=absolute --shared-index-path)
test -n "$shared"
query_inputs() {
  bazel cquery --announce_rc=false --lockfile_mode=off --config="$QUAL_CONFIG" \
    --output=starlark '--starlark:expr=json.encode([f.short_path for f in target.files.to_list()])' \
    @lint_inputs//:cc
}
query_inputs > "$RESULT_DIR/initial-inputs.json"
printf 'int second() { return 2; }\n' > "$fixture/second.cc"
git add -- tools/lint/preflight/second.cc
query_inputs > "$RESULT_DIR/added-inputs.json"
python3 - <<'EOF'
import json, os
from pathlib import Path
results = Path(os.environ['RESULT_DIR'])
def members(name):
    return {value.split('ROOT.d/', 1)[1] for value in json.loads((results / name).read_text())}
expected = {'tools/lint/preflight/first.cc', 'tools/lint/preflight/ignored.cc'}
assert members('initial-inputs.json') == expected
assert members('added-inputs.json') == expected | {'tools/lint/preflight/second.cc'}
print('Private split-index membership invalidation: PASS')
EOF
lint() {
  tools/lint.sh --bazel="$(command -v bazel)" \
    --bazel-options="--lockfile_mode=off --config=$QUAL_CONFIG" "$@" clang-format
}
status=0
lint > "$RESULT_DIR/initial-check.log" 2>&1 || status=$?
cat "$RESULT_DIR/initial-check.log"
test "$status" -ne 0
grep -F 'first.cc' "$RESULT_DIR/initial-check.log"
grep -E 'not clang-formatted|clang-format-violations' "$RESULT_DIR/initial-check.log"
lint --fix 2>&1 | tee "$RESULT_DIR/fix.log"
lint 2>&1 | tee "$RESULT_DIR/check.log"
printf 'int main() {\n       int value = 0;\n       return value;\n}\n' > "$scratch/expected"
cmp "$scratch/expected" "$fixture/first.cc"
cmp "$scratch/ignored-original" "$fixture/ignored.cc"
printf 'Adjacent style and ignore configuration through maintained caller: PASS\n'
printf 'BasedOnStyle: Google\nIndentWidth: 9\n' > "$fixture/.clang-format"
status=0
lint > "$RESULT_DIR/config-changed.log" 2>&1 || status=$?
cat "$RESULT_DIR/config-changed.log"
test "$status" -ne 0
grep -E 'not clang-formatted|clang-format-violations' "$RESULT_DIR/config-changed.log"
lint --fix 2>&1 | tee "$RESULT_DIR/refixed.log"
lint 2>&1 | tee "$RESULT_DIR/rechecked.log"
printf 'int main() {\n         int value = 0;\n         return value;\n}\n' > "$scratch/expected"
cmp "$scratch/expected" "$fixture/first.cc"
printf 'Style content invalidation: PASS\n'
rm "$fixture/second.cc"
status=0
query_inputs > "$RESULT_DIR/missing.log" 2>&1 || status=$?
cat "$RESULT_DIR/missing.log"
test "$status" -ne 0
grep -F 'existing files without symlink aliases: tools/lint/preflight/second.cc' "$RESULT_DIR/missing.log"
printf 'int second() { return 2; }\n' > "$fixture/second.cc"
ln -s first.cc "$fixture/alias.h"
git add -- tools/lint/preflight/alias.h
status=0
query_inputs > "$RESULT_DIR/unsupported.log" 2>&1 || status=$?
cat "$RESULT_DIR/unsupported.log"
test "$status" -ne 0
grep -F 'indexed regular files: tools/lint/preflight/alias.h' "$RESULT_DIR/unsupported.log"
printf 'Missing and unsupported selected files fail closed: PASS\n'
unset GIT_INDEX_FILE
bazel test --announce_rc=false --lockfile_mode=off --config="$QUAL_CONFIG" --test_output=errors \
  //tools:lint_test 2>&1 | tee "$RESULT_DIR/caller-test.log"
