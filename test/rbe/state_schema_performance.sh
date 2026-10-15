#!/bin/bash
# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -euo pipefail
export expected_source=fc2b6b435b97fed95b4c41fd5c39a535f2dee6e2
artifact_dir=$BUILDBUDDY_ARTIFACTS_DIRECTORY
# Use the existing Actions Bazelisk with the pinned repository version.
: "${STATE_PERFORMANCE_CREDENTIAL_RC:?Missing private Actions credential configuration}"
provider_bazel=$(type -P bazelisk)
test -n "$provider_bazel" && test -x "$provider_bazel"
case "$provider_bazel" in /*) ;; *) echo 'Expected an absolute provider launcher' >&2; exit 1 ;; esac
export provider_bazel
out=$(mktemp -d "$artifact_dir/state-generated-restore-benchmark.XXXXXX")
export before_source=6212e4f7b92d3090be49c21a376f1e53b7e79ce5
export before_tree=e302690ae19ac933502c72589781ba5afa9e918d
export after_tree=83600d8e0fd269de806dfc040f96551189fa723f
source_side=after
export source_side
original_head_ref=$(git symbolic-ref -q HEAD || true)
export original_head_ref
export out qualification_rc work_deadline phase_name
qualification_rc="$artifact_dir/qualification.bazelrc"
work_deadline=$(python3 -c 'import time; print(time.monotonic() + 1800)')
# shellcheck disable=SC2329
finish() {
  local result=$? cleanup_status=0 restore_status=0
  trap - EXIT
  set +e
  if [[ -f $out/codec-paths.txt ]]; then
    mapfile -t codec_paths < "$out/codec-paths.txt"
    # Deleted codec paths need no restoration once the candidate is clean.
    if git diff --name-only -z "$expected_source" -- "${codec_paths[@]}" > "$out/cleanup-paths.txt"; then
      mapfile -d '' -t changed_paths < "$out/cleanup-paths.txt"
      if (( ${#changed_paths[@]} )); then
        restore_status=0
        git restore --source="$expected_source" --staged --worktree -- "${changed_paths[@]}" || restore_status=$?
        if (( restore_status != 0 )); then cleanup_status=$restore_status; fi
      fi
    else
      cleanup_status=1
    fi
  fi
  git symbolic-ref -q HEAD > "$out/final-head-ref.txt" || true
  test "$(cat "$out/final-head-ref.txt")" = "$original_head_ref" || cleanup_status=1
  test "$(git rev-parse HEAD)" = "$expected_source" || cleanup_status=1
  git rev-parse HEAD > "$out/final-head.txt" || cleanup_status=1
  git status --porcelain --untracked-files=no > "$out/final-status.txt" || cleanup_status=1
  test ! -s "$out/final-status.txt" || cleanup_status=1
  printf '%d\n' "$result" > "$out/primary-exit.txt" || cleanup_status=1
  printf '%d\n' "$cleanup_status" > "$out/cleanup-exit.txt" || cleanup_status=1
  timeout --signal=TERM --kill-after=5s 30s python3 - <<'MANIFEST' || cleanup_status=1
import hashlib
import json
import os
from pathlib import Path
root = Path(os.environ['out'])
files = []
for f in sorted(root.rglob('*')):
    if not f.is_file():
        continue
    digest = hashlib.sha256()
    with f.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    files.append({'name': str(f.relative_to(root)), 'size': f.stat().st_size, 'sha256': digest.hexdigest()})
(root / 'manifest.json').write_text(json.dumps(files, indent=2) + '\n')
MANIFEST
  if timeout --signal=TERM --kill-after=5s 60s tar -czf "$artifact_dir/evidence.tar.gz" -C "$out" .; then
    rm -rf "$out" || cleanup_status=1
  else
    cleanup_status=1
    printf 'Archive failed; source evidence retained at %s\n' "$out" >&2
  fi
  if (( result == 0 && cleanup_status != 0 )); then result=$cleanup_status; fi
  exit "$result"
}
trap finish EXIT
test "$(git rev-parse HEAD)" = "$expected_source"
printf '%s\n' "$original_head_ref" > "$out/original-head-ref.txt"
printf 'source=%s\ncohort=state-schema-ownership-performance-actions\n' "$expected_source" > "$out/qualification.txt"
git status --porcelain --untracked-files=no > "$out/source-before.txt"
test ! -s "$out/source-before.txt"
# Hosted checkouts may contain only the requested head. Fetch this exact
# public comparison commit only when it is absent; leave refs and the worktree alone.
if ! git cat-file -e "$before_source^{commit}" 2>/dev/null; then
  timeout --signal=TERM --kill-after=5s 60s git fetch --no-tags --depth=1 origin "$before_source"
fi
git diff --name-only "$before_source" "$expected_source" > "$out/codec-paths.txt"
python3 - <<'PATHS'
import os
from pathlib import Path
assert (Path(os.environ["out"])/"codec-paths.txt").read_text().splitlines() == ['pkg/state/BUILD', 'pkg/state/capture.go', 'pkg/state/decode.go', 'pkg/state/decode_unsafe.go', 'pkg/state/encode.go', 'pkg/state/encode_unsafe.go', 'pkg/state/pretty/BUILD', 'pkg/state/pretty/pretty.go', 'pkg/state/pretty/pretty_test.go', 'pkg/state/restore.go', 'pkg/state/restore_collections.go', 'pkg/state/state.go', 'pkg/state/tests/BUILD', 'pkg/state/tests/array.go', 'pkg/state/tests/array_test.go', 'pkg/state/tests/bench.go', 'pkg/state/tests/bench_test.go', 'pkg/state/tests/load.go', 'pkg/state/tests/load_test.go', 'pkg/state/tests/map.go', 'pkg/state/tests/map_test.go', 'pkg/state/tests/record.go', 'pkg/state/tests/record_mode_norace.go', 'pkg/state/tests/record_mode_race.go', 'pkg/state/tests/reference_array.go', 'pkg/state/tests/reference_array_test.go', 'pkg/state/tests/snapshot_test.go', 'pkg/state/tests/struct.go', 'pkg/state/tests/struct_test.go', 'pkg/state/types.go', 'pkg/state/types_test.go', 'pkg/state/wire/BUILD', 'pkg/state/wire/array.go', 'pkg/state/wire/fields.go', 'pkg/state/wire/frame.go', 'pkg/state/wire/record.go', 'pkg/state/wire/value_unsafe.go', 'pkg/state/wire/value_views.go', 'pkg/state/wire/wire.go', 'pkg/state/wire/wire_test.go', 'tools/bazeldefs/go.bzl', 'tools/defs.bzl', 'tools/go_stateify/BUILD', 'tools/go_stateify/defs.bzl', 'tools/go_stateify/main.go', 'tools/go_stateify/model.go', 'tools/go_stateify/records.go', 'tools/go_stateify/restore.go']
PATHS
git diff --binary --full-index "$expected_source" "$before_source" > "$out/before.patch"
test "$(git write-tree)" = "$after_tree"
for side in before after; do
  source=$expected_source
  if [[ $side == before ]]; then source=$before_source; fi
  git cat-file -p "$source" > "$out/$side-commit.txt"
  for file in .bazelrc BUILD MODULE.bazel go.mod go.sum images/default/bazelversion pkg/compressio/BUILD pkg/compressio/compressio.go pkg/compressio/compressio_test.go pkg/compressio/nocompressio.go pkg/compressio/nocompressio_integrity_test.go pkg/compressio/nocompressio_test.go pkg/hosttid/BUILD pkg/hosttid/hosttid.go pkg/ilist/list.go pkg/segment/range.go pkg/sentry/pgalloc/BUILD pkg/sentry/pgalloc/save_restore.go pkg/sentry/socket/control/BUILD pkg/sentry/socket/control/control.go pkg/sentry/socket/plugin/cgo/BUILD pkg/sentry/socket/plugin/cgo/cgo.go pkg/sentry/socket/plugin/cgo/nocgo_stub_unsafe.go pkg/state/BUILD pkg/state/capture.go pkg/state/decode.go pkg/state/decode_unsafe.go pkg/state/encode.go pkg/state/encode_unsafe.go pkg/state/pretty/BUILD pkg/state/pretty/pretty.go pkg/state/pretty/pretty_test.go pkg/state/restore.go pkg/state/restore_collections.go pkg/state/state.go pkg/state/state_norace.go pkg/state/state_race.go pkg/state/statefile/BUILD pkg/state/statefile/statefile.go pkg/state/statefile/statefile_integrity_test.go pkg/state/statefile/statefile_test.go pkg/state/stats.go pkg/state/tests/BUILD pkg/state/tests/array.go pkg/state/tests/array_test.go pkg/state/tests/bench.go pkg/state/tests/bench_test.go pkg/state/tests/benchmark/BUILD pkg/state/tests/benchmark/bench.go pkg/state/tests/benchmark/bench_test.go pkg/state/tests/binary_test.go pkg/state/tests/bool_test.go pkg/state/tests/float_test.go pkg/state/tests/integer.go pkg/state/tests/integer_test.go pkg/state/tests/load.go pkg/state/tests/load_test.go pkg/state/tests/map.go pkg/state/tests/map_test.go pkg/state/tests/record.go pkg/state/tests/record_mode_norace.go pkg/state/tests/record_mode_race.go pkg/state/tests/reference_array.go pkg/state/tests/reference_array_test.go pkg/state/tests/register.go pkg/state/tests/register_test.go pkg/state/tests/snapshot_test.go pkg/state/tests/string_test.go pkg/state/tests/struct.go pkg/state/tests/struct_test.go pkg/state/tests/tests.go pkg/state/types.go pkg/state/types_test.go pkg/state/wire/BUILD pkg/state/wire/array.go pkg/state/wire/fields.go pkg/state/wire/frame.go pkg/state/wire/record.go pkg/state/wire/value_unsafe.go pkg/state/wire/value_views.go pkg/state/wire/wire.go pkg/state/wire/wire_test.go pkg/tcpip/BUILD pkg/tcpip/stack/BUILD pkg/tcpip/stack/packet_buffer.go pkg/tcpip/stack/registration.go pkg/tcpip/stack/route.go pkg/tcpip/tcpip.go pkg/tcpip/transport/udp/BUILD pkg/tcpip/transport/udp/codec_queue_benchmark_test.go pkg/tcpip/transport/udp/endpoint.go pkg/tcpip/transport/udp/endpoint_state.go runsc/BUILD test/kubernetes/benchmarks/BUILD tools/arch.bzl tools/bazeldefs/BUILD tools/bazeldefs/defs.bzl tools/bazeldefs/go.bzl tools/bazeldefs/go_variants.bzl tools/bazeldefs/platforms.bzl tools/bazeldefs/test_architectures.bzl tools/checkescape/BUILD tools/checkescape/checkescape.go tools/checkescape/checkescape_test.go tools/defs.bzl tools/go_stateify/BUILD tools/go_stateify/defs.bzl tools/go_stateify/main.go tools/go_stateify/model.go tools/go_stateify/records.go tools/go_stateify/restore.go tools/lint/BUILD tools/lint/defs.bzl tools/nogo/BUILD tools/nogo/check/BUILD tools/nogo/check/check.go tools/nogo/check/check_test.go tools/nogo/defs.bzl; do
    if git cat-file -e "$source:$file" 2>/dev/null; then
      git show "$source:$file" > "$out/$side-${file//\//_}.source"
    else
      printf '%s %s\n' "$side" "$file" >> "$out/absent-source-paths.txt"
    fi
  done
done
cp "$out/after-go.mod.source" "$out/go.mod.source"
cp "$out/after-images_default_bazelversion.source" "$out/images_default_bazelversion.source"
python3 - <<'HOST'
import hashlib,json,os,platform
from pathlib import Path
out=Path(os.environ['out'])
affinity=sorted(os.sched_getaffinity(0))
assert affinity and platform.machine()=='x86_64'
record={'uname':list(platform.uname()),'allowedCpus':affinity,'selectedCpu':affinity[0],
        'cpuinfo':Path('/proc/cpuinfo').read_text(),'meminfo':Path('/proc/meminfo').read_text(),
        'parentPid':os.getppid(),'gomaxprocs':1}
(out/'host.json').write_text(json.dumps(record,indent=2)+'\n')
(out/'selected-cpu.txt').write_text(str(affinity[0])+'\n')
HOST
export BENCH_CPU
BENCH_CPU=$(cat "$out/selected-cpu.txt")
test -x /usr/bin/taskset
cat > "$out/process-rss.py" <<'PROCESS_RSS'
import resource
import subprocess
import sys

result = subprocess.run(sys.argv[1:])
usage = resource.getrusage(resource.RUSAGE_CHILDREN)
print('BENCH_MAXRSS_KIB=%d' % usage.ru_maxrss, flush=True)
sys.exit(result.returncode if result.returncode >= 0 else 128 - result.returncode)
PROCESS_RSS
cat > "$out/run-under.sh" <<'AFFINITY'
#!/bin/bash
set -euo pipefail
exec /usr/bin/taskset -c "$BENCH_CPU" /bin/bash -c '
  printf "BENCH_GOMAXPROCS=%s\n" "$GOMAXPROCS"
  sed -n "/^Cpus_allowed_list:/p" /proc/self/status
  exec python3 "$@"
' codec-benchmark "$(dirname "$0")/process-rss.py" "$@"
AFFINITY
chmod +x "$out/run-under.sh"
python3 - <<'SELECTION'
import itertools,json,os
from pathlib import Path
specs = {'save_udp': ('//pkg/tcpip/transport/udp:udp_test', '^BenchmarkUDPReceiveQueueSave$/^32$', 'BenchmarkUDPReceiveQueueSave/32'), 'load_udp': ('//pkg/tcpip/transport/udp:udp_test', '^BenchmarkUDPReceiveQueueLoad$/^32$', 'BenchmarkUDPReceiveQueueLoad/32'), 'save_control': ('//pkg/state/tests/benchmark:benchmark_test', '^BenchmarkControlMessageEncoding$', 'BenchmarkControlMessageEncoding'), 'load_control': ('//pkg/state/tests/benchmark:benchmark_test', '^BenchmarkControlMessageDecoding$', 'BenchmarkControlMessageDecoding'), 'save_adoption': ('//pkg/state/tests/benchmark:benchmark_test', '^BenchmarkEncoding$/^adopt_captured$/^state$', 'BenchmarkEncoding/adopt_captured/state'), 'load_adoption': ('//pkg/state/tests/benchmark:benchmark_test', '^BenchmarkDecoding$/^adopt_captured$/^state$', 'BenchmarkDecoding/adopt_captured/state'), 'save_ptr': ('//pkg/state/tests/benchmark:benchmark_test', '^BenchmarkEncoding$/^ptr$/^state$', 'BenchmarkEncoding/ptr/state'), 'load_ptr': ('//pkg/state/tests/benchmark:benchmark_test', '^BenchmarkDecoding$/^ptr$/^state$', 'BenchmarkDecoding/ptr/state'), 'save_map': ('//pkg/state/tests/benchmark:benchmark_test', '^BenchmarkEncoding$/^map$/^state$', 'BenchmarkEncoding/map/state'), 'load_map': ('//pkg/state/tests/benchmark:benchmark_test', '^BenchmarkDecoding$/^map$/^state$', 'BenchmarkDecoding/map/state'), 'first_ordinary': ('//pkg/state/tests/benchmark:benchmark_test', '^BenchmarkFirstRestore$/^ordinary$', 'BenchmarkFirstRestore/ordinary'), 'first_retained_source': ('//pkg/state/tests/benchmark:benchmark_test', '^BenchmarkFirstRestore$/^retained_source$', 'BenchmarkFirstRestore/retained_source'), 'first_control_message': ('//pkg/state/tests/benchmark:benchmark_test', '^BenchmarkFirstRestore$/^control_message$', 'BenchmarkFirstRestore/control_message'), 'first_adoption': ('//pkg/state/tests/benchmark:benchmark_test', '^BenchmarkDecoding$/^adopt_captured$/^state$', 'BenchmarkDecoding/adopt_captured/state')}
rows={}
for side in ('before','after'):
    for workload in ['first_ordinary', 'first_retained_source', 'first_control_message', 'first_adoption']:
        owner, selector, benchmark = specs[workload]
        rows['first-'+side+'-'+workload]={'owners':{owner:{'shards':1,'timeoutSeconds':60}},'side':side,'workload':workload,'warmup':False,'firstUse':True,'benchmark':benchmark,'selector':selector}
    for workload in ['save_udp', 'load_udp', 'save_control', 'load_control', 'save_adoption', 'load_adoption', 'save_ptr', 'load_ptr', 'save_map', 'load_map']:
        owner, selector, benchmark = specs[workload]
        rows['warmup-'+side+'-'+workload]={'owners':{owner:{'shards':1,'timeoutSeconds':60}},'side':side,'workload':workload,'warmup':True,'firstUse':False,'benchmark':benchmark,'selector':selector}
orders=list(itertools.permutations(('before','after')))*6
for block,order in enumerate(orders):
    for side in order:
        for workload in ['save_udp', 'load_udp', 'save_control', 'load_control', 'save_adoption', 'load_adoption', 'save_ptr', 'load_ptr', 'save_map', 'load_map']:
            owner, selector, benchmark = specs[workload]
            rows['sample-%02d-%s-%s'%(block,side,workload)]={'owners':{owner:{'shards':1,'timeoutSeconds':60}},'side':side,'workload':workload,'block':block,'warmup':False,'firstUse':False,'benchmark':benchmark,'selector':selector}
assert len(rows)==268
(Path(os.environ['out'])/'selection.json').write_text(json.dumps(rows,indent=2)+'\n')
(Path(os.environ['out'])/'orders.txt').write_text(''.join(' '.join(order)+'\n' for order in orders))
SELECTION
python3 --version > "$out/python-version.txt"
cat > "$qualification_rc" <<'RC'
build --config=rbe
build --config=x86_64
build --nostamp
build --local_resources=cpu=4
# Match the established 16-GiB Actions coordinator budget.
build --local_resources=memory=4096
build --loading_phase_threads=4
build --experimental_throttle_remote_action_building
build --remote_download_outputs=toplevel
test --config=rbe-local-tests
test --test_env=GOMAXPROCS=1
test --test_env=GODEBUG=inittrace=1
test --nocache_test_results
test --runs_per_test=1
test --flaky_test_attempts=1
common --noannounce_rc
common --color=no
common --curses=no
RC
cp "$qualification_rc" "$out/qualification.bazelrc"
# shellcheck disable=SC2329
phase_bazel() {
  "$provider_bazel" --host_jvm_args=-Xmx8g --host_jvm_args=-XX:ActiveProcessorCount=4 --bazelrc="$STATE_PERFORMANCE_CREDENTIAL_RC" --bazelrc="$qualification_rc" "$@"
}
bazel() (
  set -e
  local arg raw_bep result=0 capture_status=0 requested_bep='' call_name=$phase_name
  local -a startup_args=() command_args=()
  while [[ $1 == --bazelrc=* ]]; do
    startup_args+=("$1")
    shift
  done
  printf '%s %s\n' "$phase_name" "$1" >> "$out/bazel-calls.txt"
  for arg in "$@"; do
    case "$arg" in
      --build_event_json_file=*) requested_bep=${arg#*=} ;;
      *) command_args+=("$arg") ;;
    esac
  done
  export call_name actual_command
  printf '%s\n' "${startup_args[@]}" "$@" > "$out/$call_name-arguments.txt"
  raw_bep=$(mktemp)
  local raw_stdout="$out/$call_name-bazel-stdout.txt"
  actual_command=${command_args[0]}
  trap 'rm -f "$raw_bep"' EXIT
  command_args=("${command_args[0]}" "--build_event_json_file=$raw_bep" "${command_args[@]:1}")
  if [[ $actual_command == test || $actual_command == build ]]; then command_args+=("--execution_log_compact_file=$out/$call_name-execution.binpb.zst"); fi
  # Bound Bazel itself so this wrapper can reduce complete partial events
  # after interruption. Raw options may contain credentials and stay temporary.
  local remaining
  remaining=$(remaining_work)
  if (( remaining > 0 )); then
    timeout --signal=INT --kill-after=30s "${remaining}s" bash -c 'phase_bazel "$@"' state-binary-time-bazel "${startup_args[@]}" "${command_args[@]}" </dev/null > "$raw_stdout" 2> "$out/$call_name-bazel-stderr.txt" || result=$?
  else
    result=124
    : > "$raw_stdout" || capture_status=1
    printf 'Shared work deadline exhausted.\n' > "$out/$call_name-bazel-stderr.txt" || capture_status=1
  fi
  printf '%d\n' "$result" > "$out/$call_name-bazel-exit.txt" || capture_status=1
  timeout --signal=TERM --kill-after=5s 45s python3 - "$raw_bep" "$requested_bep" <<'CAPTURE' || capture_status=$?
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse
out = Path(os.environ['out'])
def digest_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()
events = []
truncated_tail = False
with Path(sys.argv[1]).open() as raw:
    for line in raw:
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            # Preserve complete events from an interrupted writer, but fail capture.
            truncated_tail = True
            break
keep = ('expanded', 'configured', 'completed', 'aborted', 'namedSetOfFiles', 'testResult', 'testSummary', 'finished', 'buildMetrics')
kept = [event for event in events if any(key in event for key in keep)]
text = ''.join(json.dumps(event) + '\n' for event in kept)
(out / (os.environ['call_name'] + '-events.jsonl')).write_text(text)
if sys.argv[2]:
    Path(sys.argv[2]).write_text(text)
# Preserve every shard's materialized evidence before outcome assertions.
# Partial/missing outputs remain explicit and can be joined through saved CAS
# references; no test is repeated to repair a capture-only failure.
copies = []
selection = json.loads((out / 'selection.json').read_text()).get(os.environ['call_name'], {'owners': {}})
expected_owners = selection['owners']
for event in events:
    if 'testResult' not in event:
        continue
    identity = event['id']['testResult']
    label = identity['label']
    assert label in expected_owners, identity
    relative = label[2:].replace(':', '/').lstrip('/')
    shard = identity.get('shard', 1)
    shard_count = expected_owners[label]['shards']
    suffix = '/shard_%s_of_%s' % (shard, shard_count) if shard_count > 1 else ''
    relative_output = relative + suffix
    local = Path('bazel-testlogs') / relative_output
    directory = out / 'test-outputs' / os.environ['call_name'] / relative / ('%s-%s-%s' % (identity.get('run', 1), shard, identity.get('attempt', 1)))
    directory.mkdir(parents=True, exist_ok=True)
    for item in event['testResult'].get('testActionOutput', []):
        name = PurePosixPath(item['name'])
        assert not name.is_absolute() and '..' not in name.parts, item
        uri = item.get('uri', '')
        # A transitioned test may use a different configuration than either
        # the bazel-testlogs symlink or its alias's default executable output.
        # Inspect only this owner's exact shard/output path under native Bazel
        # configuration directories, and require the actual BEP content digest.
        if uri.startswith('file:'):
            candidates = [Path(unquote(urlparse(uri).path))]
        else:
            candidates = [local / item['name']]
            candidates.extend(sorted(Path('bazel-out').glob('*/testlogs/' + relative_output + '/' + item['name'])))
        record = {'identity': identity, 'name': item['name'], 'uri': uri, 'materialized': False}
        source = None
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if uri.startswith('bytestream:'):
                digest, size = uri.rsplit('/', 2)[-2:]
                if candidate.stat().st_size != int(size) or digest_file(candidate) != digest:
                    continue
            source = candidate
            break
        if source is not None:
            record['materializedPath'] = str(source)
            destination = directory / item['name'].replace('/', '_')
            shutil.copyfile(source, destination)
            copied_size = destination.stat().st_size
            copied_sha = digest_file(destination)
            digest = item.get('digest')
            if uri.startswith('bytestream:'):
                digest, size = uri.rsplit('/', 2)[-2:]
                assert copied_size == int(size), item
            if digest:
                assert copied_sha == digest, item
            record.update(materialized=True, saved=str(destination.relative_to(out)), sha256=copied_sha, size=copied_size)
        copies.append(record)
(out / (os.environ['call_name'] + '-test-output-copies.json')).write_text(json.dumps(copies, indent=2) + '\n')
assert kept, 'Missing build events'
assert not truncated_tail, 'Raw BEP ended with an incomplete JSON event'
option_event, = [event['optionsParsed'] for event in events if 'optionsParsed' in event]
prefixes = ('--cache_test_results=', '--run_under=', '--test_timeout=', '--test_filter=', '--test_sharding_strategy=', '--flaky_test_attempts=', '--runs_per_test=', '--jobs=', '--spawn_strategy=', '--cpu=', '--platforms=', '--host_platform=', '--test_arg=', '--strategy=', '--build_tag_filters=', '--remote_accept_cached=', '--remote_upload_local_results=', '--remote_download_outputs=', '--local_resources=', '--loading_phase_threads=', '--remote_default_exec_properties=', '--cxxopt=', '--extra_execution_platforms=', '--define=')
flags = {'--remote_accept_cached', '--noremote_accept_cached', '--remote_local_fallback', '--noremote_local_fallback', '--remote_upload_local_results', '--noremote_upload_local_results', '--keep_going', '--nokeep_going', '--remote_cache_compression', '--build_tests_only', '--nobuild_tests_only', '--cache_test_results', '--nocache_test_results', '--define=rbe=true'}
controls = [arg for arg in option_event['cmdLine'] if arg.startswith(prefixes) or arg in flags]
(out / (os.environ['call_name'] + '-build-controls.json')).write_text(json.dumps(controls, indent=2) + '\n')
assert os.environ['actual_command'] in ('run', 'build', 'test'), os.environ['actual_command']
assert {'--jobs=400', '--spawn_strategy=remote', '--noremote_local_fallback', '--define=rbe=true', '--host_platform=//tools/bazeldefs:rbe_linux_amd64'} <= set(controls), controls
assert not any(arg in controls for arg in ('--noremote_accept_cached', '--remote_accept_cached=false')), controls
if os.environ['actual_command'] != 'test':
    assert not any('testResult' in event or 'testSummary' in event for event in events), 'Unexpected test execution'
else:
    assert {'--strategy=TestRunner=local', '--runs_per_test=1', '--flaky_test_attempts=1'} <= set(controls), controls
    assert not any(arg.startswith(('--test_timeout=', '--test_sharding_strategy=', '--test_filter=')) for arg in controls), controls
    assert '--run_under=' + str(out / 'run-under.sh') in controls
    import re
    import xml.etree.ElementTree as ET
    for label in expected_owners:
        result, = [e['testResult'] for e in events if e.get('id',{}).get('testResult',{}).get('label')==label and 'testResult' in e]
        assert result['status']=='PASSED' and result['executionInfo']['strategy']=='local', result
        report, = [v for v in copies if v['identity']['label']==label and v['name']=='test.xml' and v['materialized']]
        document=ET.parse(out/report['saved'])
        assert not any(document.findall('.//'+tag) for tag in ('failure','error','skipped'))
        assert all(int(node.get(key,'0'))==0 for node in document.iter() for key in ('failures','errors','skipped'))
        log, = [v for v in copies if v['identity']['label']==label and v['name']=='test.log' and v['materialized']]
        text=(out/log['saved']).read_text().replace('\x16','')
        assert re.search(r'^PASS$',text,re.M) and not re.search(r'^FAIL(?:\s|$)',text,re.M), label
    assert any(arg in controls for arg in ('--nocache_test_results', '--cache_test_results=0', '--cache_test_results=false', '--cache_test_results=no')), controls

CAPTURE
  printf '%d\n' "$capture_status" > "$out/$call_name-capture-exit.txt" || capture_status=1
  if [[ -f $out/$call_name-bazel-stdout.txt ]]; then cat "$out/$call_name-bazel-stdout.txt" || capture_status=1; fi
  cat "$out/$call_name-bazel-stderr.txt" >&2 || capture_status=1
  if (( result != 0 )); then return "$result"; fi
  return "$capture_status"
)
remaining_work() {
  python3 -c 'import os,time; print(max(0,int(float(os.environ["work_deadline"])-time.monotonic())))'
}
export -f phase_bazel bazel remaining_work
run_phase() {
  phase_name=$1
  shift
  local result=0 capture_status=0 remaining
  remaining=$(remaining_work)
  if (( remaining > 0 )); then
    timeout --signal=INT --kill-after=30s "${remaining}s" "$@" > "$out/$phase_name-stdout.txt" 2> "$out/$phase_name-stderr.txt" || result=$?
  else
    result=124
    : > "$out/$phase_name-stdout.txt" || capture_status=1
    printf 'Shared work deadline exhausted.\n' > "$out/$phase_name-stderr.txt" || capture_status=1
  fi
  printf '%d\n' "$result" > "$out/$phase_name-exit.txt" || capture_status=1
  cat "$out/$phase_name-stderr.txt" || capture_status=1
  if (( result != 0 )); then return "$result"; fi
  return "$capture_status"
}
python3 - <<'LAUNCHER'
import hashlib
import json
import os
from pathlib import Path
path = Path(os.environ['provider_bazel'])
record = {'path': str(path), 'resolvedPath': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
(Path(os.environ['out']) / 'provider-launcher.json').write_text(json.dumps(record, indent=2) + '\n')
LAUNCHER
set_source() {
  local requested=$1
  if [[ $requested != "$source_side" ]]; then
    if [[ $source_side != after ]]; then
      git apply --index --reverse "$out/$source_side.patch"
    fi
    if [[ $requested != after ]]; then
      git apply --index "$out/$requested.patch"
    fi
    source_side=$requested
  fi
  local expected=$after_tree
  if [[ $source_side == before ]]; then expected=$before_tree; fi
  test "$(git write-tree)" = "$expected"
  git diff --quiet
  printf '%s %s %s\n' "$phase_name" "$source_side" "$expected" >> "$out/source-states.txt"
}
run_bench() {
  local name=$1 side=$2 benchtime=$3 workload=$4 owner filter
  case "$workload" in
    save_udp) owner=//pkg/tcpip/transport/udp:udp_test; filter='^BenchmarkUDPReceiveQueueSave$/^32$' ;;
    load_udp) owner=//pkg/tcpip/transport/udp:udp_test; filter='^BenchmarkUDPReceiveQueueLoad$/^32$' ;;
    save_control) owner=//pkg/state/tests/benchmark:benchmark_test; filter='^BenchmarkControlMessageEncoding$' ;;
    load_control) owner=//pkg/state/tests/benchmark:benchmark_test; filter='^BenchmarkControlMessageDecoding$' ;;
    save_adoption) owner=//pkg/state/tests/benchmark:benchmark_test; filter='^BenchmarkEncoding$/^adopt_captured$/^state$' ;;
    load_adoption) owner=//pkg/state/tests/benchmark:benchmark_test; filter='^BenchmarkDecoding$/^adopt_captured$/^state$' ;;
    save_ptr) owner=//pkg/state/tests/benchmark:benchmark_test; filter='^BenchmarkEncoding$/^ptr$/^state$' ;;
    load_ptr) owner=//pkg/state/tests/benchmark:benchmark_test; filter='^BenchmarkDecoding$/^ptr$/^state$' ;;
    save_map) owner=//pkg/state/tests/benchmark:benchmark_test; filter='^BenchmarkEncoding$/^map$/^state$' ;;
    load_map) owner=//pkg/state/tests/benchmark:benchmark_test; filter='^BenchmarkDecoding$/^map$/^state$' ;;
    first_ordinary) owner=//pkg/state/tests/benchmark:benchmark_test; filter='^BenchmarkFirstRestore$/^ordinary$' ;;
    first_retained_source) owner=//pkg/state/tests/benchmark:benchmark_test; filter='^BenchmarkFirstRestore$/^retained_source$' ;;
    first_control_message) owner=//pkg/state/tests/benchmark:benchmark_test; filter='^BenchmarkFirstRestore$/^control_message$' ;;
    first_adoption) owner=//pkg/state/tests/benchmark:benchmark_test; filter='^BenchmarkDecoding$/^adopt_captured$/^state$' ;;
    *) return 1 ;;
  esac
  phase_name=$name
  set_source "$side"
  bazel test --build_tag_filters= --test_output=errors \
    --run_under="$out/run-under.sh" --test_env="BENCH_CPU=$BENCH_CPU" \
    --test_arg=-test.run='^$' \
    "--test_arg=-test.bench=$filter" \
    --test_arg="-test.benchtime=$benchtime" --test_arg=-test.benchmem \
    --test_arg=-test.count=1 --test_arg=-test.cpu=1 \
    "$owner" > "$out/$phase_name-stdout.txt" 2> "$out/$phase_name-stderr.txt"
}
phase_name=bazel-version
run_phase bazel-version bash -c 'phase_bazel version --gnu_format' </dev/null
phase_name=sdk-version
GOTOOLCHAIN=local bazel run @io_bazel_rules_go//go -- version > "$out/sdk-version-stdout.txt" 2> "$out/sdk-version-stderr.txt"
run_phase versions python3 - <<'VERSIONS'
import os
from pathlib import Path
out=Path(os.environ['out'])
assert 'bazel '+(out/'images_default_bazelversion.source').read_text().strip() in (out/'bazel-version-stdout.txt').read_text().splitlines()
version=next(line.split()[1] for line in (out/'go.mod.source').read_text().splitlines() if line.startswith('go '))
assert 'go version go'+version+' linux/amd64' in (out/'sdk-version-bazel-stdout.txt').read_text().splitlines()
VERSIONS
# Compile both sources and the reporting tool before any measured sample.
phase_name=build-after
bazel build //pkg/state/tests/benchmark:benchmark_test //pkg/tcpip/transport/udp:udp_test @org_golang_x_perf//cmd/benchstat > "$out/build-after-stdout.txt" 2> "$out/build-after-stderr.txt"
phase_name=build-before
set_source before
bazel build //pkg/state/tests/benchmark:benchmark_test //pkg/tcpip/transport/udp:udp_test > "$out/build-before-stdout.txt" 2> "$out/build-before-stderr.txt"
for side in before after; do
  for workload in first_ordinary first_retained_source first_control_message first_adoption; do
    run_bench first-$side-$workload "$side" 1x "$workload"
  done
  for workload in save_udp load_udp save_control load_control save_adoption load_adoption save_ptr load_ptr save_map load_map; do
    run_bench warmup-$side-$workload "$side" 100ms "$workload"
  done
done
block=0
while read -r -a order; do
  for side in "${order[@]}"; do
    for workload in save_udp load_udp save_control load_control save_adoption load_adoption save_ptr load_ptr save_map load_map; do
      printf -v name 'sample-%02d-%s-%s' "$block" "$side" "$workload"
      run_bench "$name" "$side" 1s "$workload"
    done
  done
  block=$((block + 1))
done < "$out/orders.txt"
phase_name=restore-candidate
set_source after
run_phase samples python3 - <<'SAMPLES'
import json,os,re
from pathlib import Path
out=Path(os.environ['out'])
selection=json.loads((out/'selection.json').read_text())
rows=[]
files={'before':[],'after':[]}
for name,info in selection.items():
    required={info['benchmark']}
    owner,=info['owners']
    events=[json.loads(line) for line in (out/(name+'-events.jsonl')).read_text().splitlines() if line.strip()]
    event,=[event for event in events if 'testResult' in event]
    result=event['testResult']
    assert result['status']=='PASSED' and result['executionInfo']['strategy']=='local',result
    assert not result.get('cachedLocally') and not result.get('executionInfo',{}).get('cachedRemotely')
    assert all(event['id']['testResult'].get(k,1)==1 for k in ('run','shard','attempt'))
    completed,=[e['completed'] for e in events if e['id'].get('targetCompleted',{}).get('label')==owner and e.get('completed',{}).get('testTimeoutSeconds')]
    assert int(completed['testTimeoutSeconds'])==60
    copies=json.loads((out/(name+'-test-output-copies.json')).read_text())
    log,=[v for v in copies if v['name']=='test.log' and v['materialized']]
    text=(out/log['saved']).read_text()
    assert 'BENCH_GOMAXPROCS=1' in text
    assert re.search(r'^Cpus_allowed_list:\s*'+os.environ['BENCH_CPU']+r'\s*$',text,re.M)
    rss, = re.findall(r'^BENCH_MAXRSS_KIB=([0-9]+)$', text, re.M)
    assert int(rss) > 0
    samples=[]
    for line in text.splitlines():
        if not re.match(r'^Benchmark\S+\s+[0-9]+\s+',line): continue
        parts=line.split();bench=re.sub(r'-1$','',parts[0]);assert bench in required,bench
        metrics={parts[i+1]:float(parts[i]) for i in range(2,len(parts),2)}
        assert metrics['ns/op']>0 and {'B/op','allocs/op'}<=metrics.keys()
        if bench == 'BenchmarkControlMessageDecoding': assert metrics['wire-B/op']>0
        assert int(parts[1]) > 0, (name, bench, parts[1])
        if info['firstUse']: assert int(parts[1]) == 1, (name, parts[1])
        samples.append({'benchmark':bench,'iterations':int(parts[1]),'metrics':metrics,'line':line})
    assert {v['benchmark'] for v in samples}==required and len(samples)==len(required),(name,samples)
    assert not re.search(r'^=== RUN   Test',text,re.M),'Unexpected unit tests'
    rows.append({'phase':name,**info,'maxRssKiB':int(rss),'samples':samples,'log':log,'initTrace':[line for line in text.splitlines() if line.startswith('init ') and 'gvisor.dev/gvisor' in line]})
    if not info['warmup'] and not info['firstUse']:
        files[info['side']].append('pkg: gvisor.dev/gvisor/'+owner[2:].split(':')[0])
        files[info['side']].extend(v['line'] for v in samples)
for workload in {row['workload'] for row in rows}:
    wire_sizes={sample['metrics']['wire-B/op'] for row in rows if row['workload']==workload for sample in row['samples']}
    assert len(wire_sizes)==1,(workload,wire_sizes)
for workload in ('save_udp','load_udp','save_ptr','load_ptr','save_adoption','load_adoption'):
    digests=set()
    for row in rows:
        if row['workload']!=workload: continue
        text=(out/row['log']['saved']).read_text()
        values=re.findall(r'^(?:queue|state)-wire-sha256\[[^]]+\]: ([0-9a-f]{64})$',text,re.M)
        assert len(values)==1,(row['phase'],values)
        digests.update(values)
    assert len(digests)==1,(workload,digests)
assert len(rows)==268
(out/'samples.json').write_text(json.dumps(rows,indent=2)+'\n')
for side,lines in files.items():
    assert len([line for line in lines if line.startswith('Benchmark')])==120
    (out/(side+'.bench')).write_text('goos: linux\ngoarch: amd64\n'+ '\n'.join(lines)+'\n')
SAMPLES
phase_name=benchstat
bazel run --execution_log_compact_file="$out/$phase_name-execution.binpb.zst" @org_golang_x_perf//cmd/benchstat -- "$out/before.bench" "$out/after.bench" > "$out/benchstat-stdout.txt" 2> "$out/benchstat-stderr.txt"
run_phase native-sizes python3 - <<'NATIVE_SIZES'
import json,os
from pathlib import Path
out=Path(os.environ['out'])
records=[]
for side in ('before','after'):
    events=[json.loads(line) for line in (out/('build-'+side+'-events.jsonl')).read_text().splitlines() if line.strip()]
    for event in events:
        for item in event.get('namedSetOfFiles',{}).get('files',[]):
            if item['name'].startswith(('pkg/state/tests/benchmark/','pkg/tcpip/transport/udp/')):
                records.append({'side':side,**item})
(out/'native-output-metadata.json').write_text(json.dumps(records,indent=2)+'\n')
NATIVE_SIZES
phase_name="source-after"
test "$(git write-tree)" = "$after_tree"
git status --porcelain --untracked-files=no > "$out/source-after.txt"
test ! -s "$out/source-after.txt"
timeout --signal=TERM --kill-after=5s 30s bash -c 'phase_bazel shutdown' </dev/null > "$out/shutdown.txt" 2> "$out/shutdown-stderr.txt"
printf '0\n' > "$out/gate-exit.txt"
