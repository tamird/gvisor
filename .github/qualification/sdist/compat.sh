#!/bin/bash
set -euo pipefail
out="$RUNNER_TEMP/publication"
git checkout --quiet --detach "$INSTALL_SOURCE"
export USE_BAZEL_VERSION
USE_BAZEL_VERSION=$(cat images/default/bazelversion)
rc="$RUNNER_TEMP/current-release.bazelrc"
unset BUILDBUDDY_API_KEY
s=0
bazelisk --bazelrc="$rc" test --spawn_strategy=local --strategy=TestRunner=remote --remote_executor=grpcs://remote.buildbuddy.io --remote_cache=grpcs://remote.buildbuddy.io --remote_default_exec_properties=OSFamily=linux --remote_default_exec_properties=Arch=amd64 --remote_default_exec_properties=workload-isolation-type=oci --modify_execution_info='^TestRunner$=+no-remote-cache' --nocache_test_results --remote_download_outputs=all --nobuild_event_json_file_path_conversion --build_event_json_file="$RUNNER_TEMP/compat.events" --test_output=errors --//sandboxexec/sandbox/python:release_version=2026.10.29.2 //test/release:sdist_py37_install_qualification > "$RUNNER_TEMP/compat.log" 2>&1 || s=$?
printf '%s
' "$s" > "$out/compat.exit"
python3 - "$RUNNER_TEMP/compat.log" "$out/compat.json" <<'PYCOMP'
import json,re,sys
from pathlib import Path
s=Path(sys.argv[1]).read_text(errors="replace")
test=Path("bazel-testlogs/test/release/sdist_py37_install_qualification/test.log")
if test.exists(): s+="\n"+test.read_text(errors="replace")
rows=[l[:300] for l in s.splitlines() if re.search(r'ERROR:|Error:|FAILED|not found|No such file|assertion|SyntaxError',l,re.I) and not re.search(r'https?://|token|credential|secret|header',l,re.I)]
install=[json.loads(l.removeprefix("SDIST_INSTALL ")) for l in s.splitlines() if l.startswith("SDIST_INSTALL ")]
Path(sys.argv[2]).write_text(json.dumps({"installs":install,"diagnostics":rows[-12:]},indent=2)+"\n")
PYCOMP
exit "$s"
