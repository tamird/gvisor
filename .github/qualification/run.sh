#!/bin/bash
set -euo pipefail
fixture="$PWD/tools/lint/preflight"
scratch=$(mktemp -d "$RUNNER_TEMP/clang-format-rules-lint.XXXXXX")
test ! -e "$fixture"
cleanup() { rm -rf "$fixture" "$scratch"; }
trap cleanup EXIT
mkdir -p "$fixture/nested"
printf 'BasedOnStyle: Google\nIndentWidth: 7\n' > "$fixture/.clang-format"
printf 'int main() {\nint value=0;\nreturn value;\n}\n' > "$fixture/nested/first.cc"
printf 'int f() {return 0;}\n' > "$fixture/nested/first.c"
printf 'struct Header {int field;};\n' > "$fixture/nested/first.h"
printf 'filegroup(name = "fixture", srcs = ["nested/first.cc", "nested/first.c", "nested/first.h", ".clang-format"])\n' > "$fixture/BUILD"
flags=(--lockfile_mode=off --config="$QUAL_CONFIG" --remote_download_outputs=all --aspects=//tools/lint:clang_format_probe.bzl%clang_format_probe --@aspect_rules_lint//lint:fail_on_violation)
check() { bazel build "${flags[@]}" --output_groups=rules_lint_human //tools/lint/preflight:fixture; }
patch_file=bazel-bin/tools/lint/preflight/fixture.AspectRulesLintClangFormat.patch
patch_format() {
  bazel build "${flags[@]}" --@aspect_rules_lint//lint:fix --output_groups=rules_lint_patch //tools/lint/preflight:fixture
  test -s "$patch_file"
  cp "$patch_file" "$RESULT_DIR/$1.patch"
  git apply --check "$patch_file"
  git apply "$patch_file"
}
status=0
check > "$RESULT_DIR/initial-check.log" 2>&1 || status=$?
cat "$RESULT_DIR/initial-check.log"
test "$status" -ne 0
grep -F 'code should be clang-formatted' "$RESULT_DIR/initial-check.log"
patch_format canonical-fix 2>&1 | tee "$RESULT_DIR/patch-fix.log"
check 2>&1 | tee "$RESULT_DIR/recheck.log"
printf 'int main() {\n       int value = 0;\n       return value;\n}\n' > "$scratch/expected"
cmp "$scratch/expected" "$fixture/nested/first.cc"
for source in first.c first.cc first.h; do grep -F "a/tools/lint/preflight/nested/$source" "$RESULT_DIR/canonical-fix.patch"; done
printf 'Canonical declared check fails; C/CC/header patch applies; nested style recheck passes: PASS\n'
# The source selector cannot see a configuration absent from declared attributes.
printf 'BasedOnStyle: Google\nIndentWidth: 9\n' > "$fixture/nested/.clang-format"
check 2>&1 | tee "$RESULT_DIR/undeclared-config-check.log"
cmp "$scratch/expected" "$fixture/nested/first.cc"
bazel run --lockfile_mode=off --config="$QUAL_CONFIG" //tools/lint:clang_format_probe_tool -- -i "$fixture/nested/first.cc" 2>&1 | tee "$RESULT_DIR/original-path-fix.log"
if cmp -s "$scratch/expected" "$fixture/nested/first.cc"; then echo 'Expected undeclared adjacent configuration contrast' >&2; exit 1; fi
printf 'int main() {\n         int value = 0;\n         return value;\n}\n' > "$scratch/expected-nine"
cmp "$scratch/expected-nine" "$fixture/nested/first.cc"
printf 'Known boundary reproduced: undeclared adjacent config affects original-path fix but not declared check\n'
rm "$fixture/nested/.clang-format"
patch_format reset-fix 2>&1 | tee "$RESULT_DIR/reset-fix.log"
check 2>&1 | tee "$RESULT_DIR/final-check.log"
bazel run --lockfile_mode=off --config="$QUAL_CONFIG" //tools/lint:clang_format_probe_tool -- --version 2>&1 | tee "$RESULT_DIR/llvm-version.log"
printf 'Prototype canonical check/patch/apply/recheck complete; production config/caller migration remains unresolved\n'
