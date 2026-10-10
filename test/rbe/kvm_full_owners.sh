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

# Run the two original complete RC KVM owners after the named crash cases did
# not reproduce. Preserve every declared case, shard and per-action timeout.
set -euo pipefail
[[ $(git rev-parse HEAD) == "$QUALIFICATION_COMMIT" ]]
# Exported by the existing Actions coordinator.
# shellcheck disable=SC2154
[[ $qualification_root_bazel == true && $GITHUB_ACTIONS == true && $RUNNER_ENVIRONMENT == github-hosted ]]
# Both established root fixtures run below the same unprivileged coordinator.
export qualification_root_bazel=false
out="$RUNNER_TEMP/qualification/kvm-full-owners"
mkdir -p "$out"
coordinator_uid=$(id -u)
coordinator_gid=$(id -g)
raw_events=""
# Keep complete raw options in a private directory outside uploaded artifacts.
raw_directory=$(mktemp -d "$RUNNER_TEMP/kvm-full-bep.XXXXXX")
# shellcheck disable=SC2329
finish() {
  local status=$?
  trap - EXIT
  if ! rm -rf -- "$raw_directory"; then
    if (( status == 0 )); then status=1; fi
  fi
  if ! git diff --exit-code > "$out/final-source.diff"; then
    if (( status == 0 )); then status=1; fi
  fi
  if ! sudo -n chown -hR "$coordinator_uid:$coordinator_gid" "$out"; then
    if (( status == 0 )); then status=1; fi
  fi
  printf '%s\n' "$status" > "$out/final-exit.txt"
  exit "$status"
}
trap finish EXIT
uname -a > "$out/host-kernel.txt"
lscpu > "$out/host-lscpu.txt"
cat /proc/cpuinfo > "$out/host-cpuinfo.txt"
cat /proc/self/limits > "$out/host-limits.txt"
git cat-file -p HEAD > "$out/commit.txt"
for path in go.mod MODULE.bazel images/default/bazelversion test/runner/defs.bzl test/runner/runner_test.bzl test/runner/main.go test/runner/gtest/gtest.go test/syscalls/BUILD test/syscalls/linux/cgroup2.cc test/syscalls/linux/socket_inet_loopback_isolated.cc test/rbe/tcg/BUILD test/rbe/tcg/defs.bzl test/rbe/tcg/build_image.sh test/rbe/tcg/run.sh test/rbe/tcg/init.sh pkg/sentry/platform/kvm/bluepill_amd64.go pkg/sentry/platform/kvm/bluepill_amd64_unsafe.go pkg/sentry/platform/kvm/bluepill_unsafe.go pkg/sentry/platform/kvm/machine_amd64.go pkg/sigframe/sigframe_amd64.s pkg/sigframe/sigframe_amd64_unsafe.go pkg/ring0/entry_amd64.s tools/bazeldefs/test_architectures.bzl test/rbe/local_root.sh; do
  git show "HEAD:$path" > "$out/${path//\//_}.source"
done
base_owners=(
  //test/syscalls:cgroup2_test_runsc_kvm
  //test/syscalls:socket_inet_loopback_isolated_test_runsc_kvm
)
labels=()
for owner in "${base_owners[@]}"; do labels+=("${owner}_rc_kvm"); done
printf '%s\n' "${labels[@]}" > "$out/owners.txt"
work_deadline=$(python3 -c 'import time; print(time.monotonic()+5100)')
remaining() { python3 -c 'import sys,time; print(max(0,int(float(sys.argv[1])-time.monotonic())))' "$work_deadline"; }
# Query owning declarations, including original deadlines and the RC payload
# alias. Actual compact TestRunner placement and ELF inputs remain result proof.
# The exported shell function is evaluated in the child bash.
# shellcheck disable=SC2016
timeout --signal=INT --kill-after=30s "$(remaining)s" \
  bash -c 'bazel query --output=xml --xml:default_values "$1"' _ \
  "set(${labels[*]})" > "$out/attributes.xml"
python3 - "$out" <<'ATTRIBUTES'
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
out = Path(sys.argv[1])
rules = {node.attrib['name']: node for node in ET.parse(out/'attributes.xml').iter('rule')}
for label in (out/'owners.txt').read_text().splitlines():
    rule = rules[label]
    def value(name):
        field, = [node for node in rule if node.attrib.get('name') == name]
        return field.attrib.get('value')
    assert value('timeout') == 'long', (label, value('timeout'))
    assert int(value('shard_count')) == (4 if 'cgroup2_test' in label else 8)
    if label.endswith('_rc_kvm'):
        assert value('payload') == label[:-len('_rc_kvm')] + '_amd64', label
        assert value('image') == '//test/rbe/tcg:amd64_rc_guest', label
ATTRIBUTES
run_phase() {
  local phase=$1 status=0 capture_status=0 seconds
  shift
  printf 'Starting diagnostic phase %s\n' "$phase"
  printf '%s\n' "$@" > "$out/$phase.arguments.txt"
  raw_events=$(mktemp "$raw_directory/events.XXXXXX")
  seconds=$(remaining)
  if (( seconds > 0 )); then
    timeout --signal=INT --kill-after=30s "${seconds}s" \
      bash -c 'bazel "$@"' _ "$@" \
      "--build_event_json_file=$raw_events" \
      > "$out/$phase.stdout.txt" 2> "$out/$phase.stderr.txt" || status=$?
  else
    status=124
  fi
  printf '%s\n' "$status" > "$out/$phase.exit.txt"
  python3 - "$raw_events" "$out/$phase.events.jsonl" <<'BEP' || capture_status=$?
import json
from pathlib import Path
import sys
source, target = map(Path, sys.argv[1:])
keys = {'id','children','started','finished','configured','completed','testResult','testSummary','aborted','namedSetOfFiles','buildMetrics'}
count = 0
with source.open() as stream, target.open('w') as output:
    for line in stream:
        event = json.loads(line)
        if 'started' in event:
            event['started'] = {key: value for key, value in event['started'].items() if key in {'uuid','command','startTime','startTimeMillis'}}
        safe = {key: value for key, value in event.items() if key in keys}
        text = json.dumps(safe)
        assert 'x-buildbuddy-api-key' not in text.lower()
        output.write(text+'\n')
        count += 1
assert count, 'No complete build events'
BEP
  rm -f "$raw_events"
  raw_events=""
  printf '%s\n' "$capture_status" > "$out/$phase.capture-exit.txt"
  printf 'Finished diagnostic phase %s: bazel=%s capture=%s\n' "$phase" "$status" "$capture_status"
  if (( status == 0 )); then status=$capture_status; fi
  return "$status"
}

# Resolve/build the declared guest before running either arm. A missing index
# is an incomplete preparation, not a reason to spend more host-only samples.
prebuild_status=0
run_phase guest-image build --config=rbe --config=x86_64 --strip=never \
  //test/rbe/tcg:amd64_rc_guest || prebuild_status=$?
if (( prebuild_status != 0 )); then exit "$prebuild_status"; fi
# Preserve the canonical RC profile's single local slot. Each action boots its
# own guest; no test filter, sharding override or timeout override is supplied.
run_phase full-owners test --config=rbe --config=x86_64 \
  --strategy=TestRunner=local --run_under=//test/rbe:local_root \
  --//tools/bazeldefs:local_test_architecture= \
  --//tools/bazeldefs:local_test_backend=local --//tools/bazeldefs:page_size=4k \
  --build_tag_filters= --test_tag_filters= --strip=never --keep_going \
  --incompatible_sandbox_hermetic_tmp=false --local_test_jobs=1 \
  --nocache_test_results --runs_per_test=1 --flaky_test_attempts=1 \
  --test_output=errors --zip_undeclared_test_outputs --test_env=GTEST_FILTER= \
  "${labels[@]}"
