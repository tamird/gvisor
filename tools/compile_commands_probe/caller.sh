#!/usr/bin/env bash
set -euo pipefail
cd "$BUILD_WORKSPACE_DIRECTORY"
shim=$(mktemp -d "$RUNNER_TEMP/compile-commands-caller.XXXXXX")
trap 'rm -rf "$shim"' EXIT
cat > "$shim/bazel" <<'SHIM'
#!/usr/bin/env bash
set -euo pipefail
mapfile -d '' -t options < <(python3 - <<'OPTIONS'
import json, os, sys
for option in json.loads(os.environ['QUALIFICATION_BAZEL_OPTIONS_JSON']):
    sys.stdout.write(option + '\0')
OPTIONS
)
exec "$QUALIFICATION_BAZEL" "$1" "${options[@]}" "${@:2}"
SHIM
chmod +x "$shim/bazel"
export PATH="$shim:$PATH"
status=0
case "$1" in
  devcontainer) bash tools/devcontainer-init.sh || status=$? ;;
  lint) LINT_CACHE_DIR="$RUNNER_TEMP/bazel-qualification/lint-cache" bash tools/lint.sh --bazel="$shim/bazel" clang-tidy || status=$? ;;
  *) exit 2 ;;
esac
python3 - "$1" "$status" <<'VERIFY'
import json,sys
from pathlib import Path
entries=json.loads(Path('compile_commands.json').read_text())
missing=[entry['file'] for entry in entries if not (Path(entry['directory']) / entry['file']).is_file()]
print(json.dumps({'caller':sys.argv[1], 'callerExitStatus':int(sys.argv[2]), 'databaseEntries':len(entries),'missingEntryFiles':len(missing),'missingExamples':missing[:10]}))
assert entries and not missing
VERIFY
exit "$status"
