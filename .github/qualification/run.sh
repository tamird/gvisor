#!/bin/bash
set -euo pipefail
fixture=$(mktemp -d "$RUNNER_TEMP/format-ownership-preflight.XXXXXX")
link="$PWD/format-ownership-preflight"
test ! -e "$link"
test ! -L "$link"
trap 'rm -f "$link"; rm -rf "$fixture"' EXIT
ln -s "$fixture" "$link"
mkdir "$fixture/child" "$fixture/bazel-ignored"
cat > "$fixture/BUILD" <<'EOF'
load("//tools/lint:ownership.bzl", "format_sources")
package(default_applicable_licenses = ["//:license"])
format_sources()
genrule(name = "generated", outs = ["generated.go"], cmd = "touch $@")
EOF
cat > "$fixture/child/BUILD" <<'EOF'
load("//tools/lint:ownership.bzl", "format_sources")
package(default_applicable_licenses = ["//:license"])
format_sources()
EOF
printf 'package parent\n' > "$fixture/parent.go"
printf 'package child\n' > "$fixture/child/child.go"
printf 'fail("Bazel output subtree must not be loaded")\n' > "$fixture/bazel-ignored/BUILD"
printf 'package ignored\n' > "$fixture/bazel-ignored/output.go"
query_sources() {
  bazel cquery --announce_rc=false --lockfile_mode=off --config="$QUAL_CONFIG" \
    --output=starlark '--starlark:expr=json.encode([f.short_path for f in target.files.to_list()])' \
    //format-ownership-preflight:format_sources
}
query_sources > "$RESULT_DIR/initial-sources.json"
printf 'package parent\n' > "$fixture/added.go"
query_sources > "$RESULT_DIR/added-sources.json"
python3 - <<'EOF'
import json, os
from pathlib import Path
results = Path(os.environ['RESULT_DIR'])
prefix = 'format-ownership-preflight/'
initial = set(json.loads((results / 'initial-sources.json').read_text()))
expected = {prefix + name for name in ('BUILD', 'parent.go', 'child/BUILD', 'child/child.go')}
assert initial == expected, (initial, expected)
added = set(json.loads((results / 'added-sources.json').read_text()))
assert added == expected | {prefix + 'added.go'}, added
print('Parent/child ownership and visibility: PASS')
print('New ordinary source membership: PASS')
print('Declared generated output and bazel-* subtree exclusion: PASS')
EOF
mkdir "$fixture/native-only"
printf 'package(default_applicable_licenses = ["//:license"])\nfilegroup(name = "ordinary", srcs = ["native.go"])\n' > "$fixture/native-only/BUILD"
printf 'package native\n' > "$fixture/native-only/native.go"
status=0
query_sources > "$RESULT_DIR/missing-hook.log" 2>&1 || status=$?
cat "$RESULT_DIR/missing-hook.log"
test "$status" -ne 0
grep -F "no such target '//format-ownership-preflight/native-only:format_sources'" "$RESULT_DIR/missing-hook.log"
printf 'New native-only package requires declaration: PASS\n'
