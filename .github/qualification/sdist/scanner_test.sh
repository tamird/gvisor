#!/bin/bash
set -euo pipefail
git checkout --quiet --detach "$SCANNER_SOURCE"
export USE_BAZEL_VERSION
USE_BAZEL_VERSION=$(cat images/default/bazelversion)
out="$RUNNER_TEMP/publication"
rc="$RUNNER_TEMP/current-release.bazelrc"
status=0
bazelisk --bazelrc="$rc" test --remote_cache=grpcs://remote.buildbuddy.io --bes_backend=grpcs://remote.buildbuddy.io --bes_results_url=https://app.buildbuddy.io/invocation/ --spawn_strategy=local --nocache_test_results --nobuild_event_json_file_path_conversion --build_event_json_file="$RUNNER_TEMP/scanner.events" --test_arg=-test.v --test_output=all //tools/licensecheck:licensecheck_test >"$RUNNER_TEMP/scanner.log" 2>&1 || status=$?
echo "$status" >"$out/scanner-tests.exit"
python3 - "$RUNNER_TEMP/scanner.events" "$out/scanner-tests.json" "$RUNNER_TEMP/scanner.log" <<'PYTEST'
import json,re,sys,xml.etree.ElementTree as ET
from pathlib import Path
p=Path(sys.argv[1])
events=[json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
target=Path("bazel-testlogs/tools/licensecheck/licensecheck_test/test.xml")
cases=[c.attrib.get("name","") for c in ET.parse(target).iter("testcase")] if target.exists() else []
rows=Path(sys.argv[3]).read_text(errors="replace").splitlines()
diags=[re.sub(r"https?://[^\s),;]+","[URL]",l)[:600] for l in rows if re.search(r"error|fail|unknown|invalid",l,re.I) and not re.search(r"header|secret|token|credential|api[-._]key|authorization",l,re.I)]
observed=[l.strip() for l in rows if re.match(r"^\s*--- (?:PASS|FAIL|SKIP): Test(?:PipHub|Wheel|LicensePolicy)",l)]
Path(sys.argv[2]).write_text(json.dumps({"tested":observed,"invocation":next((e["started"].get("uuid") for e in events if "started" in e),None),"cases":cases,"diagnostics":diags[-12:]},indent=2)+"\n")
PYTEST
test "$status" = 0
