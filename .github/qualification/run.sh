#!/bin/bash
set -euo pipefail
test "$SOURCE" = 4db24d95066ae2c6288741c940b64c011b3e1cd5
test "$(git rev-parse HEAD)" = "$SOURCE"
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
status=0
flags=("--config=$QUAL_CONFIG" --jobs=3 --local_test_jobs=2 --remote_executor= --remote_cache= --bes_backend=)
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}")
run() {
  local name="$1" rc=0
  shift
  "$@" 2>&1 | tee "$RESULT_DIR/$name.log" || rc=$?
  printf '%s\t%s\t%s\n' "$(git rev-parse HEAD)" "$name" "$rc" >> "$RESULT_DIR/commands.tsv"
  if test "$rc" != 0; then status=1; fi
  return "$rc"
}
trap '"${bazel_cmd[@]}" shutdown || true' EXIT
targets='//pkg/sentry/platform/systrap:systrap_test //pkg/shim/v1:shim_test //pkg/shim/v1/proc:proc_test //runsc/cmd:cmd_test //runsc/specutils:specutils_test'
opts='--nocache_test_results --test_output=errors --test_sharding_strategy=disabled --test_env=GO_TEST_WRAP_TESTV=1'
run owners make "${make_args[@]}" test "TARGETS=$targets" "OPTIONS=$opts" || true
logs="$("${bazel_cmd[@]}" info bazel-testlogs)"
plan_dir="$(dirname "${BASH_SOURCE[0]}")"
capture() {
  python3 - "$logs" "$RESULT_DIR/$1" "$plan_dir/required-cases.json" "$2" <<'PY'
import json
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

logs, output, required_file = map(Path, sys.argv[1:4])
selected = sys.argv[4].split(',')
required = json.loads(required_file.read_text())
owners = {
    'systrap': 'pkg/sentry/platform/systrap/systrap_test',
    'shim': 'pkg/shim/v1/shim_test',
    'proc': 'pkg/shim/v1/proc/proc_test',
    'cmd': 'runsc/cmd/cmd_test',
    'specutils': 'runsc/specutils/specutils_test',
}
report = {}
for owner in selected:
    directory = output / owner
    directory.mkdir(parents=True, exist_ok=True)
    for filename in ('test.xml', 'test.log'):
        shutil.copy2(logs / owners[owner] / filename, directory / filename)
    cases = ET.parse(directory / 'test.xml').getroot().findall('.//testcase')
    assert cases, owner
    failed = [case.attrib for case in cases if any(case.find(tag) is not None for tag in ('failure', 'error'))]
    assert not failed, (owner, failed)
    skipped = [dict(case.attrib, reason={'attributes': case.find('skipped').attrib, 'text': case.find('skipped').text}) for case in cases if case.find('skipped') is not None]
    for name in required[owner]:
        matches = [case for case in cases if case.get('name', '') == name or case.get('name', '').startswith(name + '/')]
        assert matches, (owner, name, 'required regression did not run')
        assert not [case.attrib for case in matches if case.find('skipped') is not None], (owner, name, 'required regression skipped')
    report[owner] = {'cases': len(cases), 'requiredTests': required[owner], 'skippedOtherCases': skipped}
    print(owner, len(cases), 'cases; required regressions passed without skips;', len(skipped), 'other skips')
(output / 'case-report.json').write_text(json.dumps(report, indent=2) + '\n')
PY
}
capture native systrap,shim,proc,cmd,specutils || status=1
if test "$QUAL_ARCH" = amd64; then
  run race make "${make_args[@]}" test TARGETS='//pkg/shim/v1:shim_test //pkg/shim/v1/proc:proc_test' "OPTIONS=$opts --config=race" || true
  capture race shim,proc || status=1
fi
run lint make "${make_args[@]}" lint || true
if test "$QUAL_ARCH" = amd64; then
  run lint-cc make "${make_args[@]}" lint-cc || true
else
  printf '%s\n' 'Full C++ lint runs on AMD64. This plan does not claim ARM64 C++ lint passed. The proposal changes Go test infrastructure only.' > "$RESULT_DIR/arm-cpp-scope.txt"
fi
printf '%s\n' 'This is test-only process infrastructure. Native owner suites run on both architectures; changed regression cases must execute without skips. Any unrelated capability skips are recorded explicitly. Shim owners also run with the race detector on AMD64. No production performance claim is made.' > "$RESULT_DIR/validation-scope.txt"
exit "$status"
