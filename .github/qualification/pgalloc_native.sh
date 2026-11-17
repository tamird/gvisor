#!/bin/bash
set -euo pipefail
w="$RUNNER_TEMP/pgalloc-native"
results="$w/results"
status=0
bazel_cmd=("$w/bin/bazel" --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
flags=("--config=$QUAL_CONFIG" --jobs=3 --strip=never --remote_executor= --remote_cache= --bes_backend=)
make_args=(DOCKER_BUILD=false "BAZEL=$w/bin/bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc" "BAZEL_OPTIONS=${flags[*]}")
run() {
  local name="$1" rc=0
  shift
  "$@" 2>&1 | tee "$results/$name.log" || rc=$?
  printf '%s\t%s\t%s\n' "$(git rev-parse HEAD)" "$name" "$rc" >> "$results/commands.tsv"
  if test "$rc" != 0; then status=1; fi
  return "$rc"
}
trap '"$w/bin/bazel" --host_jvm_args=-Xmx4g shutdown || true' EXIT
printf '%s\n' "$SOURCE" > "$results/source.txt"
printf '%s\n' "$BASE" > "$results/base.txt"
uname -a > "$results/worker.txt"
getconf PAGESIZE > "$results/host-page-size.txt"
run owners make "${make_args[@]}" test TARGETS='//pkg/sentry/pgalloc:pgalloc_test //pkg/safecopy:safecopy_test' OPTIONS='--nocache_test_results --strategy=TestRunner=local --test_env=GO_TEST_WRAP_TESTV=1' || true
if test -f .build_events.json; then cp .build_events.json "$results/owners.bep"; fi
logs="$("${bazel_cmd[@]}" info bazel-testlogs)"
for package in sentry/pgalloc safecopy; do
  name="${package##*/}"
  for kind in xml log; do
    path="$logs/pkg/$package/${name}_test/test.$kind"
    if test -f "$path"; then cp "$path" "$results/$name.test.$kind"; else status=1; fi
  done
done
if ! python3 - "$results" <<'PYXML'
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
root=Path(sys.argv[1]);files=[root/'pgalloc.test.xml',root/'safecopy.test.xml']
for path in files:
    cases=ET.parse(path).getroot().findall('.//testcase')
    assert cases, str(path)
    failed=[case.attrib for case in cases if any(case.find(tag) is not None for tag in ('skipped','failure','error'))]
    assert not failed, (str(path),failed)
    print(path.name,len(cases),'PASS, zero skips')
    if path.name=='pgalloc.test.xml':
        names={case.get('name','') for case in cases}
        for expected in ('TestAllocateAndCommit','TestAllocateRecycledDiskBacked','TestZeroDiskBackedSkipsZeroUnwritablePage','TestZeroDiskBackedPanicsOnNonzeroUnwritablePage'):
            assert any(name==expected or name.startswith(expected+'/') for name in names),(expected,sorted(names))
PYXML
then status=1; fi
cat > "$results/reused-lint-evidence.txt" <<'EVIDENCE'
Exact source 7dfa385ef0dd6286eaff54ee1c988db289f4f044 was already linted in
https://github.com/tamird/gvisor/actions/runs/44176619047
AMD64: make lint and make lint-cc passed (artifact 14371321961).
ARM64: make lint passed; candidate and exact base bf5df9a1c9211afe4dbd7267c4423e9ae575f7fa
both failed make lint-cc with the same io_uring.cc:63 include-cleaner warning
for __NR_io_uring_enter (artifact 14372945060). That failure remains unresolved.
This focused run does not repeat or relabel those lint results.
EVIDENCE
# This rebase adds only runtime page-size type conversions to the approved
# implementation. Its EOF/protection fixtures now use actual host page sizes.
# It does not add a new zeroing mechanism or change the already-reviewed
# allocation policy; historical performance evidence retains its old source.
cat > "$results/performance-scope.txt" <<'SCOPE'
The rebase preserves the approved allocation algorithm and zeroing calls.
Changes relative to f05d6b9093 are page-size type conversions, mmap fixture
page-size initialization and DCO metadata. Earlier source-specific performance
measurements are not fresh measurements of this commit. If review identifies
a new runtime change, run matched benchmarks before publication.
SCOPE
exit "$status"
