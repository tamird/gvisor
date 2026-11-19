#!/usr/bin/env bash
set -euo pipefail
cd "$BUILD_WORKSPACE_DIRECTORY"
mapfile -d '' -t options < <(python3 - <<'OPTIONS'
import json, os, sys
for option in json.loads(os.environ['QUALIFICATION_BAZEL_OPTIONS_JSON']):
    sys.stdout.write(option + '\0')
OPTIONS
)
target=//test/gpu:gpu_driver_versions_proto
"$QUALIFICATION_BAZEL" build "${options[@]}" --aspects=//tools:cc_compile_inputs.bzl%cc_compile_inputs --output_groups=cc_compile_inputs "$target"
generator_options=(--resolve "--bazel-command=$QUALIFICATION_BAZEL")
for option in "${options[@]}"; do generator_options+=("--bazelopt=$option"); done
"$QUALIFICATION_BAZEL" run "${options[@]}" --platforms=@platforms//host //tools/compile_commands -- "${generator_options[@]}" "$target"
check_files() {
python3 - "$1" <<'VERIFY'
import json,sys
from pathlib import Path
entries=json.loads(Path('compile_commands.json').read_text())
missing=[entry['file'] for entry in entries if not (Path(entry['directory']) / entry['file']).is_file()]
print(json.dumps({'phase':sys.argv[1], 'entries':len(entries),'missing':len(missing),'examples':missing[:5]}))
if sys.argv[1]=='afterMaterialization': assert entries and not missing
VERIFY
}
check_files afterBazelRun
"$QUALIFICATION_BAZEL" build "${options[@]}" --aspects=//tools:cc_compile_inputs.bzl%cc_compile_inputs --output_groups=cc_compile_inputs "$target"
check_files afterMaterialization
