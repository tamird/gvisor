#!/usr/bin/env bash
set -euo pipefail
tool=$(realpath "$1")
cd "$BUILD_WORKSPACE_DIRECTORY"
bazel=${QUALIFICATION_BAZEL:?}
results="$RUNNER_TEMP/bazel-qualification"
trap 'for events in "$results"/{native,aspect}-prerequisites-events.json; do [[ ! -f "$events" ]] || cat "$events"; done' EXIT
mapfile -d '' -t options < <(python3 - <<'OPTIONS'
import json, os, sys
for option in json.loads(os.environ['QUALIFICATION_BAZEL_OPTIONS_JSON']):
    sys.stdout.write(option + '\0')
OPTIONS
)
target=//tools/compile_commands_probe:root
bin=$("$bazel" info "${options[@]}" bazel-bin)
test ! -e "$bin/pkg/compile_commands_probe/generated/source.cc"
test ! -e "$bin/pkg/compile_commands_probe/generated/header.h"
"$bazel" build "${options[@]}" --remote_download_outputs=all   --output_groups=compilation_prerequisites_INTERNAL_   --build_event_publish_all_actions   --build_event_json_file="$results/native-prerequisites-events.json" "$target"
test -f "$bin/pkg/compile_commands_probe/generated/header.h"
test -f "$bin/pkg/compile_commands_probe/_virtual_includes/dep/probe/header.h"
native_source=false
if [[ -f "$bin/pkg/compile_commands_probe/generated/source.cc" ]]; then native_source=true; fi
"$bazel" build "${options[@]}" --remote_download_outputs=all   --aspects=//tools:cc_compile_inputs.bzl%cc_compile_inputs   --output_groups=cc_compile_inputs --build_event_publish_all_actions   --build_event_json_file="$results/aspect-prerequisites-events.json" "$target"
test -f "$bin/pkg/compile_commands_probe/generated/source.cc"
tool_options=(--resolve "--bazel-command=$bazel"
  --bazelsupopt=--max_idle_secs=10800
  --bazelopt=--copt=-DCOMPILE_COMMANDS_PROBE=1)
for option in "${options[@]}"; do tool_options+=("--bazelopt=$option"); done
"$tool" "${tool_options[@]}" "$target"
python3 - "$native_source" <<'VERIFY'
from collections import Counter
import json, os
from pathlib import Path
import sys
root = Path.cwd().resolve()
entries = json.loads((root / 'compile_commands.json').read_text())
assert len(entries) >= 2, entries
files = [((Path(entry['directory']) / entry['file']).resolve(), entry) for entry in entries]
assert len({path for path, _ in files}) == 2, files
assert any(path == root / 'tools/compile_commands_probe/root.cc' for path, _ in files), files
assert any(str(path).endswith('/pkg/compile_commands_probe/generated/source.cc') for path, _ in files), files
for path, entry in files:
    assert path.is_file(), str(path)
    assert '-DCOMPILE_COMMANDS_PROBE=1' in entry['arguments'], entry
    assert not (Path(entry['directory']) / entry['output']).exists(), entry['output']
counts = {}
for name in ('native', 'aspect'):
    events = [json.loads(line) for line in Path(os.environ['RUNNER_TEMP'], 'bazel-qualification', name + '-prerequisites-events.json').read_text().splitlines()]
    counts[name] = Counter(event['action']['type'] for event in events if 'action' in event)
    assert counts[name].get('CppCompile', 0) == 0, counts
print(json.dumps({'nativeDependencyGeneratedSource': sys.argv[1] == 'true', 'aspectDependencyGeneratedSource': True, 'headerAndVirtualIncludeMaterialized': True, 'databaseEntries': len(entries), 'entryFilesExistRelativeToDirectory': True, 'compilerBuildOptionForwarded': True, 'startupOptionAccepted': True, 'executedActionTypes': counts}))
VERIFY
