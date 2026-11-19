#!/usr/bin/env bash
set -euo pipefail
cd "$BUILD_WORKSPACE_DIRECTORY"
file=tools/licensecheck/dependencies.yaml
original=$(mktemp "$RUNNER_TEMP/license-inventory.XXXXXX")
cp "$file" "$original"
trap 'cp "$original" "$file"; rm -f "$original"' EXIT
mapfile -d '' -t options < <(python3 - <<'OPTIONS'
import json, os, sys
for option in json.loads(os.environ['QUALIFICATION_BAZEL_OPTIONS_JSON']):
    sys.stdout.write(option + '\0')
OPTIONS
)
status=0
"$QUALIFICATION_BAZEL" run "${options[@]}" //tools/licensecheck/main:licensecheck -- --mode=fetch || status=$?
git diff -- "$file"
exit "$status"
