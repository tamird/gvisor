#!/bin/bash
set -euo pipefail
git checkout --quiet --detach "$SCANNER_SOURCE"
export USE_BAZEL_VERSION
USE_BAZEL_VERSION=$(cat images/default/bazelversion)
export MAKEFLAGS="DOCKER_BUILD=false BAZEL=$(command -v bazelisk)"
out="$RUNNER_TEMP/publication"
catalog=tools/licensecheck/dependencies.yaml
omitted="$RUNNER_TEMP/without-tomli-w.yaml"
python3 - "$catalog" "$omitted" <<'PYCATALOG'
import sys
from pathlib import Path
source=Path(sys.argv[1]).read_text()
start=source.index("- dependency: pypi/tomli_w-1.2.0-py3-none-any.whl")
finish=source.index("- dependency: ",start+1)
Path(sys.argv[2]).write_text(source[:start]+source[finish:])
PYCATALOG
rc="$RUNNER_TEMP/current-release.bazelrc"
for sample in complete omitted; do
  yaml="$catalog"
  if [[ $sample == omitted ]]; then yaml="$omitted"; fi
  status=0
  bazelisk --bazelrc="$rc" run --remote_cache=grpcs://remote.buildbuddy.io --spawn_strategy=local //tools/licensecheck/main:licensecheck -- --mode=verify --yaml="$yaml" >"$RUNNER_TEMP/license-$sample.log" 2>&1 || status=$?
  echo "$status" >"$out/license-$sample.exit"
  python3 - "$RUNNER_TEMP/license-$sample.log" "$out/license-$sample.json" <<'PYROWS'
import json,re,sys
from pathlib import Path
rows=Path(sys.argv[1]).read_text(errors="replace").splitlines()
safe=[x[:1000] for x in rows if not re.search(r"https?://|secret|credential|token|api.key|remote.header",x,re.I) and re.search(r"all [0-9]+ dependencies|pypi/|missing|obsolete|stale|ERROR:|Error:|problems|cannot run make|FAILED|mismatch",x,re.I)]
Path(sys.argv[2]).write_text(json.dumps(safe[-40:],indent=2)+"\n")
PYROWS
done
test "$(cat "$out/license-complete.exit")" = 0
test "$(cat "$out/license-omitted.exit")" != 0
grep -Fq 'pypi/tomli_w-1.2.0-py3-none-any.whl' "$out/license-omitted.json"
