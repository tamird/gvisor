#!/usr/bin/env bash
set -euo pipefail
cd "$BUILD_WORKSPACE_DIRECTORY"
scratch="$RUNNER_TEMP/pty-feedback-checks"
mkdir -p "$scratch"
export PTY_CHECK_SCRATCH="$scratch"
python3 - <<'PY'
import os
from pathlib import Path
source = Path('tools/lint.sh').read_text()
assert source.endswith('main "$@"\n')
source = source[:-len('main "$@"\n')]
assignment = 'REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"'
assert source.count(assignment) == 1
source = source.replace(assignment, 'REPO_DIR="${BUILD_WORKSPACE_DIRECTORY}"')
Path(os.environ['PTY_CHECK_SCRATCH'], 'lint-functions.sh').write_text(source)
PY
source "$scratch/lint-functions.sh"
BAZEL=("$QUALIFICATION_BAZEL")
mapfile -d '' -t BAZEL_OPTIONS < <(python3 - <<'PY'
import json, os, sys
sys.stdout.buffer.write(b''.join(x.encode() + b'\0' for x in json.loads(os.environ['QUALIFICATION_BAZEL_OPTIONS_JSON'])))
PY
)
cc_files() { printf 'test/syscalls/linux/pty.cc\0'; }
bazel_files() { printf 'test/syscalls/linux/BUILD\0'; }
check_clang_format
check_cpplint
check_buildifier
generator_options=(--resolve --replace=-fno-canonical-system-headers= "--bazel-command=${BAZEL[0]}")
for option in "${BAZEL_OPTIONS[@]}"; do generator_options+=("--bazelopt=$option"); done
run_bazel run --platforms=@platforms//host //tools/compile_commands -- \
  "${generator_options[@]}" --output="$PWD/compile_commands.json" //test/syscalls/linux:pty_test
run_bazel build --remote_download_outputs=all \
  --aspects=//tools:cc_compile_inputs.bzl%cc_compile_inputs \
  --output_groups=cc_compile_inputs //test/syscalls/linux:pty_test
clang_tidy="$(install_clang_tidy)"
export PTY_CLANG_TIDY="$clang_tidy"
sha256sum "$clang_tidy" .clang-tidy tools/clang_tidy/clang_tidy.py test/syscalls/linux/pty.cc test/syscalls/linux/BUILD
"$clang_tidy" --version
python3 - <<'PY'
import importlib.util
import json
import os
from pathlib import Path
import sys
workspace = Path.cwd()
database = workspace / 'compile_commands.json'
entries = json.loads(database.read_text())
selected = [entry for entry in entries if Path(entry['directory'], entry['file']).resolve() == workspace / 'test/syscalls/linux/pty.cc']
assert len(selected) == 1, selected
database.write_text(json.dumps(selected, indent=2))
print(json.dumps({'source': os.environ['SOURCE'], 'database_entries': len(entries), 'selected_entries': selected}))
spec = importlib.util.spec_from_file_location('clang_tidy', 'tools/clang_tidy/clang_tidy.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
run = module.subprocess.run
children = []
def record(*args, **kwargs):
    result = run(*args, **kwargs)
    children.append({'arguments': result.args, 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
    return result
module.subprocess.run = record
sys.argv = ['clang_tidy.py', '--clang-tidy=' + os.environ['PTY_CLANG_TIDY'], '--database=' + str(database), '--config-file=' + str(workspace / '.clang-tidy'), '--jobs=1']
result = module.main()
print(json.dumps({'analyzer_status': result, 'children': children}, indent=2))
assert len(children) == 1, children
assert all(child['returncode'] == 0 for child in children), children
raise SystemExit(result)
PY
