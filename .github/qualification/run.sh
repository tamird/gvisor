#!/bin/bash
set -euo pipefail
test "$SOURCE" = ab45538513c30723c6fe2cdcfc36d1a6fd96f543
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$QUAL_ARCH:$QUAL_CONFIG" = amd64:x86_64
plan_dir="$(dirname "${BASH_SOURCE[0]}")"
mkdir -p "$RESULT_DIR"
status=0
flags=("--config=$QUAL_CONFIG" --jobs=3 --local_test_jobs=2 --strip=never --nostamp --remote_executor= --remote_cache= --bes_backend=)
args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}")
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
trap 'timeout --kill-after=30s 1m "${bazel_cmd[@]}" shutdown || true' EXIT
run() {
  local name="$1" rc=0
  shift
  "$@" 2>&1 | tee "$RESULT_DIR/$name.log" || rc=$?
  printf '%s\t%s\t%s\n' "$SOURCE" "$name" "$rc" >> "$RESULT_DIR/commands.tsv"
  if test "$rc" != 0; then status=1; fi
  return "$rc"
}
capture() {
  local label="$1" phase="$2" source="$3"
  python3 - "$source" "$RESULT_DIR/$phase" "$label" <<'PY'
import json
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

source, output = map(Path, sys.argv[1:3])
output.mkdir(parents=True, exist_ok=True)
for name in ('test.xml', 'test.log'):
    shutil.copy2(source / name, output / name)
cases = ET.parse(output / 'test.xml').getroot().findall('.//testcase')
failures = [case.attrib for case in cases if any(case.find(tag) is not None for tag in ('failure', 'error'))]
skips = [dict(case.attrib, reason=case.find('skipped').text) for case in cases if case.find('skipped') is not None]
native = sys.argv[3] == "iouring_test_native"
expected_names = ['CorruptRingHeader', 'InvalidOpCodeTest', 'MMapMUnMapWork', 'MMapOffsets', 'MMapWrongOffset', 'MultiThreadedNOPTest', 'MultipleNOPTest', 'NoReadPermissionsREADVTest', 'NonNullSigsetTest', 'OverflowCQTest', 'ParamsNonZeroResv', 'QueueingNOPTest', 'READVWithInvalidSqeFieldValue', 'ReadClosedFD', 'ReadvEmptyFile', 'ReturnedParamsValues', 'SQERingBuffersWrapAroundTest', 'ShortReadREADVTest', 'SingleNOPTest', 'SingleREADVTest', 'SqeIndexArrayCacheAligned', 'ThreeREADVSingleEnterTest', 'UnsupportedFlags', 'ValidFD', 'ZeroCQEntries', 'ZeroCQEntriesLessThanSQEntries']
allowed_native_skips = {'UnsupportedFlags': {'message': 'test/syscalls/linux/io_uring.cc:143\n!IsRunningOnGvisor()\n', 'text': 'test/syscalls/linux/io_uring.cc:143\n!IsRunningOnGvisor()\n'}, 'ReturnedParamsValues': {'message': 'test/syscalls/linux/io_uring.cc:235\n!IsRunningOnGvisor()\n', 'text': 'test/syscalls/linux/io_uring.cc:235\n!IsRunningOnGvisor()\n'}, 'NonNullSigsetTest': {'message': 'test/syscalls/linux/io_uring.cc:625\n!IsRunningOnGvisor()\n', 'text': 'test/syscalls/linux/io_uring.cc:625\n!IsRunningOnGvisor()\n'}, 'OverflowCQTest': {'message': 'test/syscalls/linux/io_uring.cc:651\n!IsRunningOnGvisor()\n', 'text': 'test/syscalls/linux/io_uring.cc:651\n!IsRunningOnGvisor()\n'}}
actual_names = [case.get('name') for case in cases]
unexpected_skips = []
for case in cases:
    skipped = case.find('skipped')
    if skipped is None:
        continue
    expected_skip = allowed_native_skips.get(case.get('name')) if native else None
    if (case.get('classname') != 'IOUringTest' or expected_skip is None or
            skipped.get('message') != expected_skip['message'] or
            skipped.text != expected_skip['text']):
        unexpected_skips.append(case.attrib)
report = {'cases': len(cases), 'executed': len(cases) - len(skips), 'failures': failures,
          'skips': skips, 'unexpectedSkips': unexpected_skips,
          'expectedNativeOnlySkips': sorted(allowed_native_skips) if native else []}
(output / 'case-report.json').write_text(json.dumps(report, indent=2) + '\n')
assert sorted(actual_names) == expected_names, (actual_names, expected_names)
assert all(case.get('classname') == 'IOUringTest' for case in cases)
assert not failures and not unexpected_skips, (failures, unexpected_skips)
PY
}
python3 - "$plan_dir/iouring-native-proof/$QUAL_ARCH" "$SOURCE" <<'PROOF'
import hashlib
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
manifest = json.loads((root / 'manifest.json').read_text())
identity = manifest['identity']
assert identity['source'] == sys.argv[2] and identity['sourceVerified']
assert identity['architecture'] == 'amd64' and identity['run'] == '44206349370'
assert identity['plan'] == '1d74d97c85c573ab9d12be099233fc88ae4ea1d4'
for name, expected in manifest['files'].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
assert '\tnative\t0\n' in manifest['commands']
PROOF
capture iouring_test_native reused-native "$plan_dir/iouring-native-proof/$QUAL_ARCH"
cp "$plan_dir/iouring-native-proof/$QUAL_ARCH/manifest.json" "$RESULT_DIR/reused-native/manifest.json"
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
opts='--nocache_test_results --test_output=errors --test_sharding_strategy=disabled --flaky_test_attempts=1'
for variant in systrap_directfs systrap_shared; do
  label="iouring_test_runsc_$variant"
  run "$variant" timeout --kill-after=30s 45m make "${args[@]}" syscall-tests "TARGETS=//test/syscalls:$label" "OPTIONS=$opts" || true
  logs="$(timeout --kill-after=30s 2m "${bazel_cmd[@]}" info bazel-testlogs)"
  capture "$label" "$variant-results" "$logs/test/syscalls/$label" || status=1
done
exit "$status"
