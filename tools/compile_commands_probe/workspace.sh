#!/usr/bin/env bash
set -euo pipefail
cd "$BUILD_WORKSPACE_DIRECTORY"
results="$RUNNER_TEMP/bazel-qualification"
trap '[[ ! -f "$results/workspace-prerequisites-events.json" ]] || cat "$results/workspace-prerequisites-events.json"' EXIT
mapfile -d '' -t options < <(python3 - <<'OPTIONS'
import json, os, sys
for option in json.loads(os.environ['QUALIFICATION_BAZEL_OPTIONS_JSON']):
    sys.stdout.write(option + '\0')
OPTIONS
)
"$QUALIFICATION_BAZEL" build "${options[@]}"   --aspects=//tools:cc_compile_inputs.bzl%cc_compile_inputs   --output_groups=cc_compile_inputs   --build_event_json_file="$results/workspace-prerequisites-events.json" //test/... //tools/...
generator_options=(--resolve "--bazel-command=$QUALIFICATION_BAZEL")
for option in "${options[@]}"; do generator_options+=("--bazelopt=$option"); done
"$QUALIFICATION_BAZEL" run "${options[@]}" --platforms=@platforms//host   //tools/compile_commands -- "${generator_options[@]}" //test/... //tools/...
python3 - <<'VERIFY'
import json
from pathlib import Path
entries = json.loads(Path('compile_commands.json').read_text())
missing = [str(Path(entry['directory']) / entry['file']) for entry in entries if not (Path(entry['directory']) / entry['file']).is_file()]
print(json.dumps({'databaseEntries': len(entries), 'missingEntryFiles': len(missing), 'missingExamples': missing[:10]}))
assert entries and not missing
VERIFY
