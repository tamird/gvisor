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
  bazelisk --bazelrc="$rc" run --bes_backend=grpcs://remote.buildbuddy.io --bes_results_url=https://app.buildbuddy.io/invocation/ --nobuild_event_json_file_path_conversion --build_event_json_file="$RUNNER_TEMP/$sample.events" --remote_cache=grpcs://remote.buildbuddy.io --spawn_strategy=local //tools/licensecheck/main:licensecheck -- --mode=verify --yaml="$yaml" >"$RUNNER_TEMP/license-$sample.log" 2>&1 || status=$?
  echo "$status" >"$out/license-$sample.exit"
  python3 - "$RUNNER_TEMP/license-$sample.log" "$out/license-$sample.json" <<'PYROWS'
import json,re,sys
from pathlib import Path
rows=Path(sys.argv[1]).read_text(errors="replace").splitlines()
out=[]
for row in rows:
    if re.search(r"remote_header|bes_header|api[-._]key|secret|credential|token|authorization",row,re.I):
        continue
    row=re.sub(r"https?://[^\s),;]+","[URL]",row)
    if re.search(r"error|fatal|cannot|could not|failed|invalid|unknown|unrecognized|missing|obsolete|stale|pypi/|licensecheck:|up-to-date license entries|not found|mismatch|does not|no such|permission denied|problems",row,re.I):
        out.append(row[:1100])
if not out:
    out=[re.sub(r"https?://[^\s),;]+","[URL]",row)[:400] for row in rows[-9:] if not re.search(r"remote_header|bes_header|api[-._]key|secret|credential|token|authorization",row,re.I)]
Path(sys.argv[2]).write_text(json.dumps(out[-50:],indent=2)+"\n")
PYROWS
done
for sample in complete omitted; do
  python3 - "$RUNNER_TEMP/$sample.events" "$out/$sample-invocation.json" <<'PYBEP'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
events=[json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
start=next((e["started"].get("uuid") for e in events if "started" in e),None)
finish=next((e["finished"].get("exitCode") for e in events if "finished" in e),None)
Path(sys.argv[2]).write_text(json.dumps({"invocation":start,"bazel_exit":finish},indent=2)+"\n")
PYBEP
done
if [[ $(cat "$out/license-complete.exit") != 0 ]]; then
  status=0
  bazelisk mod show_repo @python_distribution_deps --lockfile_mode=update >"$RUNNER_TEMP/hub.log" 2>&1 || status=$?
  python3 - "$RUNNER_TEMP/hub.log" "$out/hub.json" "$status" <<'PYHUB'
import json,re,sys
from pathlib import Path
rows=Path(sys.argv[1]).read_text(errors="replace").splitlines()
attributes=[m.group(1) for l in rows if (m:=re.match(r"^\s{2,}([a-z_]+)\s*=",l))]
diags=[re.sub(r"https?://[^\s),;]+","[URL]",l)[:600] for l in rows if re.search(r"error|fail|unknown|invalid",l,re.I) and not re.search(r"header|secret|token|credential|api[-._]key|authorization",l,re.I)]
Path(sys.argv[2]).write_text(json.dumps({"status":int(sys.argv[3]),"attributes":attributes,"errors":diags[-10:]},indent=2)+"\n")
PYHUB
fi
test "$(cat "$out/license-complete.exit")" = 0
test "$(cat "$out/license-omitted.exit")" != 0
grep -Fq 'pypi/tomli_w-1.2.0-py3-none-any.whl' "$out/license-omitted.json"
