#!/bin/bash
set -euo pipefail
uname -m | tee "$RESULT_DIR/client-architecture.log"
test "$(uname -m)" = aarch64
scratch=$(mktemp -d "$RUNNER_TEMP/cc-format-preflight.XXXXXX")
fixture="$PWD/tools/lint/preflight"
parent_ignore="$(dirname "$PWD")/.clang-format-ignore"
owned_parent_ignore=0
test ! -e "$fixture"
test ! -L "$fixture"
test ! -e "$parent_ignore"
test ! -L "$parent_ignore"
cleanup() {
  rm -rf "$fixture" "$scratch"
  if [[ "$owned_parent_ignore" -eq 1 ]]; then rm -f "$parent_ignore"; fi
}
trap cleanup EXIT
mkdir -p "$fixture/nested"
export GIT_INDEX_FILE="$scratch/index"
export LINT_CACHE_DIR="$scratch/cache"
git read-tree --empty
printf 'BasedOnStyle: Google\nIndentWidth: 7\n' > "$fixture/.clang-format"
printf 'int main() {\nint value=0;\nreturn value;\n}\n' > "$fixture/nested/first.cc"
git add -- .clang-format .clang-format-ignore tools/lint/preflight/.clang-format tools/lint/preflight/nested/first.cc
printf '**/first.cc\n' > "$parent_ignore"
owned_parent_ignore=1
lint() {
  tools/lint.sh --bazel="$(command -v bazel)" \
    --bazel-options="--lockfile_mode=off --config=x86_64" "$@" clang-format
}
lint --fix 2>&1 | tee "$RESULT_DIR/bounded-fix.log"
lint 2>&1 | tee "$RESULT_DIR/bounded-check.log"
printf 'int main() {\n       int value = 0;\n       return value;\n}\n' > "$scratch/expected"
cmp "$scratch/expected" "$fixture/nested/first.cc"
printf 'Indexed root ignore boundary prevents parent ignore from skipping fixes: PASS\n'
bazel cquery --announce_rc=false --lockfile_mode=off --config=x86_64 --output=files \
  'filter("^//tools/lint:clang_format_tool$", deps(//tools/lint:format_clang))' \
  > "$RESULT_DIR/cross-fix-tool-files.txt"
python3 - <<'EOF'
import os, struct
from pathlib import Path
results = Path(os.environ['RESULT_DIR'])
paths = (results / 'cross-fix-tool-files.txt').read_text().splitlines()
assert len(paths) == 1, paths
with Path(paths[0]).open('rb') as stream:
    header = stream.read(20)
assert header[:4] == bytes((127, 69, 76, 70)), header
endian = '<' if header[5] == 1 else '>'
machine, = struct.unpack(endian + 'H', header[18:20])
assert machine == 183, machine
print('Cross-build fixer selects an AArch64 ELF executable: PASS')
EOF
bazel run --announce_rc=false --lockfile_mode=off --config="$QUAL_CONFIG" \
  //tools/lint:clang_format_tool -- --version 2>&1 | tee "$RESULT_DIR/llvm-version.log"

for config in .clang-format _clang-format .clang-format-ignore; do
  if [[ "$config" == .clang-format-ignore ]]; then
    printf 'first.cc\n' > "$fixture/nested/$config"
  else
    printf 'BasedOnStyle: Google\nIndentWidth: 9\n' > "$fixture/nested/$config"
  fi
  for action in check fix; do
    args=()
    if [[ "$action" == fix ]]; then args=(--fix); fi
    status=0
    lint "${args[@]+"${args[@]}"}" > "$RESULT_DIR/unindexed-$config-$action.log" 2>&1 || status=$?
    cat "$RESULT_DIR/unindexed-$config-$action.log"
    test "$status" -ne 0
    grep -F "Unindexed formatter configuration: tools/lint/preflight/nested/$config" "$RESULT_DIR/unindexed-$config-$action.log"
    cmp "$scratch/expected" "$fixture/nested/first.cc"
  done
  git add -- "tools/lint/preflight/nested/$config"
  lint --fix 2>&1 | tee "$RESULT_DIR/indexed-$config-fix.log"
  lint 2>&1 | tee "$RESULT_DIR/indexed-$config-check.log"
  git rm --cached -- "tools/lint/preflight/nested/$config"
  rm "$fixture/nested/$config"
  lint --fix > "$RESULT_DIR/reset-$config.log" 2>&1
  cmp "$scratch/expected" "$fixture/nested/first.cc"
  printf 'Unindexed %s blocks check/fix; indexed config restores shared behavior: PASS\n' "$config"
done
unset GIT_INDEX_FILE
tools/lint.sh --bazel="$(command -v bazel)" \
  --bazel-options="--lockfile_mode=off --config=$QUAL_CONFIG" clang-format \
  2>&1 | tee "$RESULT_DIR/full-check.log"
bazel run --announce_rc=false --lockfile_mode=off --config="$QUAL_CONFIG" \
  @buildifier_prebuilt//:buildifier -- --mode=check --lint=warn \
  --warnings=-duplicated-name,-list-append,-native-py,-native-sh-binary,-native-sh-library,-native-sh-test,-native-proto \
  MODULE.bazel tools/BUILD tools/lint/BUILD tools/lint_sources.bzl
bazel test --announce_rc=false --lockfile_mode=off --config="$QUAL_CONFIG" --test_output=errors \
  //tools:lint_test 2>&1 | tee "$RESULT_DIR/caller-test.log"
