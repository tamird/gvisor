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

# Run on a Linux coordinator; GitHub supplies these inputs as data.
set -euo pipefail
[[ $(uname -s) == Linux ]]
[[ $(git rev-parse HEAD) == "$QUALIFICATION_COMMIT" ]]
[[ $QUALIFICATION_COMMIT == 9783ac286aeb40f4673e9b6e183cc7befc462d09 ]]
[[ $QUALIFICATION_EXECUTION:$QUALIFICATION_ARCH:$QUALIFICATION_LANES:$QUALIFICATION_SYSCALL_BUCKET:$QUALIFICATION_RC_BUCKET11_PART == local:amd64:syscalls-rc:11:mmap ]]
QUALIFICATION_WORK_DEADLINE=$(python3 -c 'import time; print(time.monotonic() + 85 * 60)')
export QUALIFICATION_WORK_DEADLINE

# read consumes one line. Reject extra lines rather than losing selected lanes.
if [[ $QUALIFICATION_LANES == *$'\n'* ]]; then
  printf 'Supply qualification lanes on one line.\n' >&2
  exit 2
fi
read -r -a lanes <<< "$QUALIFICATION_LANES"
if (( ${#lanes[@]} == 0 )); then
  printf 'Select at least one qualification lane.\n' >&2
  exit 2
fi
for lane in "${lanes[@]}"; do
  # Options such as --list could otherwise succeed without running any lane.
  if [[ ! $lane =~ ^[a-z][a-z0-9-]*$ ]]; then
    printf 'Invalid lane name: %s\n' "$lane" >&2
    exit 2
  fi
done
options=("--arch=$QUALIFICATION_ARCH")
if [[ -n ${QUALIFICATION_SYSCALL_BUCKET:-} ]]; then
  options+=("--syscall-bucket=$QUALIFICATION_SYSCALL_BUCKET")
fi
if [[ ${QUALIFICATION_RC_BUCKET11_PART:-none} != none ]]; then
  options+=("--rc-bucket11-part=$QUALIFICATION_RC_BUCKET11_PART")
fi
if [[ -n ${QUALIFICATION_BENCHMARK_TARGET:-} ]]; then
  options+=("--benchmark-target=$QUALIFICATION_BENCHMARK_TARGET")
fi
temporary_files=()
GVISOR_DOCKER_NETWORK=
cleanup() {
  local status=$?
  rm -f -- "${temporary_files[@]}"
  if [[ -n $GVISOR_DOCKER_NETWORK ]]; then
    # Bazel has removed its --rm test containers before returning. An endpoint
    # left behind is a cleanup failure, not a reason to force-disconnect it.
    if ! sudo -n docker --host=unix:///var/run/docker.sock network rm "$GVISOR_DOCKER_NETWORK"; then
      if (( status == 0 )); then status=1; fi
    fi
  fi
  exit "$status"
}
trap cleanup EXIT

case "${QUALIFICATION_EXECUTION:-remote}" in
  remote) ;;
  local|remote-actions)
    if [[ $QUALIFICATION_EXECUTION == local ]]; then
      options+=(--test-execution=local)
    else
      # Hour-long guest tests need more runway than the hosted coordinator's
      # observed one-hour cap. Only the coordinator moves; all guest actions
      # retain their remote execution requirements and declared deadlines.
      if [[ $QUALIFICATION_ARCH != arm64 || ${lanes[*]} != syscalls-64k ]]; then
        printf 'Remote tests on the Actions coordinator require the ARM64 64K profile.\n' >&2
        exit 2
      fi
    fi
    # Keep the compiler and toolchain actions on the existing RBE platforms.
    # The repository version, not the runner image's default, selects Bazel.
    export USE_BAZEL_VERSION
    USE_BAZEL_VERSION=$(<images/default/bazelversion)
    command -v bazelisk
    : "${BUILDBUDDY_API_KEY:?Configure the BUILDBUDDY_API_KEY repository secret}"
    qualification_rc=$(mktemp)
    temporary_files+=("$qualification_rc")
    export qualification_rc
    # Bazel's Docker strategy runs each test as the coordinator's UID. These
    # lanes need root for their namespace fixture or KVM device; others retain
    # the nonroot server.
    # https://github.com/bazelbuild/bazel/blob/f8278f94e/src/main/java/com/google/devtools/build/lib/sandbox/DockerSandboxedSpawnRunner.java#L267-L274
    qualification_root_bazel=false
    case "$QUALIFICATION_ARCH:${lanes[*]}" in
      amd64:syscalls-rc-pilot|amd64:syscalls-rc|all:syscalls-rc|amd64:plugin-network|amd64:nftables|amd64:moby|amd64:kvm|amd64:language-goferfs|amd64:syscalls|amd64:startup|amd64:posture|amd64:portforward|amd64:root|amd64:benchmarks|arm64:docker|arm64:cpu-images|arm64:gpu-images)
        qualification_root_bazel=true
        ;;
    esac
    export qualification_root_bazel
    if [[ ${lanes[*]} == language-goferfs || ${lanes[*]} == moby || ${lanes[*]} == kvm || ${lanes[*]} == benchmarks || ${lanes[*]} == docker || ${lanes[*]} == cpu-images || ${lanes[*]} == gpu-images ]]; then
      # Docker owns routing, NAT and endpoint teardown. A user-defined bridge
      # keeps each nested daemon's firewall in its own network namespace.
      [[ -S /var/run/docker.sock ]]
      GVISOR_DOCKER_NETWORK=$(sudo -n docker --host=unix:///var/run/docker.sock network create \
        --driver bridge "gvisor-qualification-${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}")
      [[ $GVISOR_DOCKER_NETWORK =~ ^[0-9a-f]{64}$ ]]
      export GVISOR_DOCKER_NETWORK
    fi
    {
      printf '%s\n' \
        'build:buildbuddy_remote_executor --remote_executor=grpcs://remote.buildbuddy.io' \
        'build:buildbuddy_remote_executor --remote_cache=grpcs://remote.buildbuddy.io' \
        'build:buildbuddy_remote_executor --bes_backend=grpcs://remote.buildbuddy.io' \
        'build:buildbuddy_remote_executor --bes_results_url=https://app.buildbuddy.io/invocation/'
      # Root Bazel does not inherit the GitHub environment. Record the checked
      # checkout identity explicitly for both coordinator users.
      printf 'build:buildbuddy_remote_executor --build_metadata=COMMIT_SHA=%s\n' "$QUALIFICATION_COMMIT"
      printf 'build:buildbuddy_remote_executor --build_metadata=REPO_URL=%s/%s\n' "$GITHUB_SERVER_URL" "$GITHUB_REPOSITORY"
      printf 'build:buildbuddy_remote_executor --build_metadata=BRANCH_NAME=%s\n' "$GITHUB_REF_NAME"
      printf 'build:buildbuddy_remote_executor --remote_header=x-buildbuddy-api-key=%s\n' "$BUILDBUDDY_API_KEY"
      printf 'build:buildbuddy_remote_executor --bes_header=x-buildbuddy-api-key=%s\n' "$BUILDBUDDY_API_KEY"
      if [[ $QUALIFICATION_EXECUTION == remote-actions ]]; then
        printf '%s\n' \
          'build:buildbuddy_remote_executor --remote_download_outputs=minimal' \
          'test:buildbuddy_remote_executor --nocache_test_results' \
          'test:buildbuddy_remote_executor --runs_per_test=1' \
          'test:buildbuddy_remote_executor --flaky_test_attempts=1' \
          'test:buildbuddy_remote_executor --zip_undeclared_test_outputs'
      fi
    } > "$qualification_rc"
    if [[ $QUALIFICATION_EXECUTION == remote-actions ]]; then
      QUALIFICATION_WORK_DEADLINE=$(python3 -c 'import time; print(time.monotonic()+7200)')
      export QUALIFICATION_WORK_DEADLINE
    fi
    unset BUILDBUDDY_API_KEY
    # Capture spawn placement without including the credential RC in artifacts.
    mkdir -p "$RUNNER_TEMP/qualification"
    bazel() {
      local argument result=0 capture_status=0 raw_events="" raw_events_dir="" events_output="" cohort_dir="" remaining
      local -a evidence=()
      # Startup options may precede the command. Queries perform no spawns and
      # do not accept execution-log options; each build/test keeps its own log.
      for argument in "$@"; do
        if [[ $argument == --* ]]; then
          continue
        fi
        if [[ $argument == build || $argument == test ]]; then
          evidence=(
            "--execution_log_compact_file=$(mktemp "$RUNNER_TEMP/qualification/execution-XXXXXX.binpb")"
          )
        fi
        if [[ $argument == test && ( $QUALIFICATION_EXECUTION == remote-actions || ${QUALIFICATION_RC_BUCKET11_PART:-none} != none ) ]]; then
          # Keep raw parsed options out of the uploaded artifact directory.
          raw_events_dir=$(mktemp -d)
          raw_events="$raw_events_dir/events.jsonl"
          events_output=$(mktemp "$RUNNER_TEMP/qualification/guest-events-XXXXXX.jsonl")
          evidence+=("--build_event_json_file=$raw_events")
        fi
        break
      done
      if [[ $QUALIFICATION_EXECUTION == remote-actions && $argument == test ]]; then
        # Execute the complete maintained profile with its declared emulator
        # settings; retain the graph and native contracts for comparison.
        cohort_dir="$RUNNER_TEMP/qualification/tcg-full-profile"
        local pattern_file="" source_file
        mkdir -p "$cohort_dir"
        printf '%s\n' "$@" > "$cohort_dir/original-arguments.txt"
        for source_file in "$@"; do
          if [[ $source_file == --target_pattern_file=* ]]; then
            [[ -z $pattern_file ]]
            pattern_file=${source_file#*=}
          fi
        done
        [[ -n $pattern_file ]]
        git status --porcelain --untracked-files=no > "$cohort_dir/source-before.txt"
        [[ ! -s "$cohort_dir/source-before.txt" ]]
        git cat-file -p HEAD > "$cohort_dir/commit.txt"
        for source_file in .bazelrc MODULE.bazel go.mod go.sum test/runner/defs.bzl test/runner/runner_test.bzl test/runner/main.go test/syscalls/BUILD test/syscalls/linux/BUILD test/syscalls/linux/processes.cc test/syscalls/linux/ping_socket.cc test/syscalls/linux/semaphore.cc test/syscalls/linux/socket_ipv4_udp_unbound_loopback_nogotsan.cc test/syscalls/linux/socket_generic_stress.cc test/syscalls/linux/ip_socket_test_util.cc test/util/test_util.cc test/util/test_main.cc test/rbe/tcg/defs.bzl test/rbe/tcg/BUILD test/rbe/tcg/run.sh test/rbe/tcg/init.sh test/rbe/qualify.sh test/rbe/unit_matrix.py tools/bazeldefs/go.bzl tools/bazeldefs/platforms.bzl; do
          git show "HEAD:$source_file" > "$cohort_dir/${source_file//\//_}.source"
        done
        python3 - "$pattern_file" "$RUNNER_TEMP/qualification/syscalls-64k-selection/actions.json" "$cohort_dir" <<'TCG_SELECTION'
import hashlib
import json
from pathlib import Path
import sys
pattern, actions_path, out = map(Path, sys.argv[1:])
selected = pattern.read_text().splitlines()
assert len(selected) == len(set(selected)) == 661
assert hashlib.sha256(('\n'.join(sorted(selected))+'\n').encode()).hexdigest() == '60cfb4e88a92cb9c4953b449f2e92ab2fad9e5066dc36f431ab60360f2ce9f68'
raw = json.loads(actions_path.read_text())
labels = {str(row['id']):row['label'] for row in raw['targets']}
rows = sorted([{'label':labels[str(row['targetId'])], 'args':row['arguments'], 'properties':{p['key']:p.get('value','') for p in row.get('executionInfo',[])}, 'executionPlatform':row.get('executionPlatform')} for row in raw['actions'] if row['mnemonic']=='TestRunner'], key=lambda row:json.dumps(row,sort_keys=True))
assert len(rows)==1286 and {row['label'] for row in rows}==set(selected)
stress_labels={label for label in selected if label.startswith('//test/syscalls:socket_stress_test_runsc_')}
assert len(stress_labels)==3
original_rows=[row for row in rows if row['label'] not in stress_labels]
for label in sorted(stress_labels):
    changed=[row for row in rows if row['label']==label]
    assert len(changed)==33 and all(row==changed[0] for row in changed)
    original_rows.extend(changed[:8])
# Compare the unchanged route after accounting for the new emulation default.
for row in original_rows:
    assert row['args'][2:3] == ['--strace=false'], row
original_rows = [dict(row, args=row['args'][:2] + row['args'][3:]) for row in original_rows]
original_rows.sort(key=lambda row:json.dumps(row,sort_keys=True))
assert len(original_rows)==1211
assert hashlib.sha256(json.dumps(original_rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()=='e694f292dd67497deaf2b5e5e5f12d03f573395ff7ed6e62321e1923f12a7bd4'
args=(out/'original-arguments.txt').read_text().splitlines()
assert '--strip=never' in args and '--keep_going' in args
assert not any(arg.startswith(('--test_filter=','--test_arg=','--test_timeout=','--test_sharding_strategy=','--run_under=','--runs_per_test=','--flaky_test_attempts=')) for arg in args)
(out/'full-targets').write_text('\n'.join(selected)+'\n')
(out/'routing.json').write_text(json.dumps(rows,indent=2)+'\n')
query_labels=[]
for outer in selected:
    assert outer.endswith('_64k_tcg'),outer
    owner=outer[:-len('_64k_tcg')]
    query_labels.extend([owner,owner+'_64k_arm64',outer,owner+'_rc_tcg',owner+'_rc_kvm'])
(out/'attributes.query').write_text('set('+' '.join('"'+label+'"' for label in query_labels)+')\n')
TCG_SELECTION
        remaining=$(python3 -c 'import os,time; print(max(0,int(float(os.environ["QUALIFICATION_WORK_DEADLINE"])-time.monotonic())))')
        (( remaining > 0 ))
        timeout --signal=INT --kill-after=30s "${remaining}s" \
          "$(command -v bazelisk)" --bazelrc="$qualification_rc" query \
          --noannounce_rc --output=xml --xml:default_values \
          --query_file="$cohort_dir/attributes.query" > "$cohort_dir/attributes.xml"
        python3 - "$cohort_dir" <<'TCG_ATTRIBUTES'
import json
from pathlib import Path
import sys
from xml.etree import ElementTree
out=Path(sys.argv[1])
rules={row.attrib['name']:row for row in ElementTree.parse(out/'attributes.xml').iter('rule')}
def attribute(label,name):
    value,=[child for child in rules[label] if child.attrib.get('name')==name]
    if value.tag=='list':return [entry.attrib['value'] for entry in value]
    assert value.tag in {'string','int','boolean','label'},(label,name,value.tag)
    return value.attrib['value']
overrides={'processes_test':'eternal','socket_stress_test':'eternal','ping_socket_test':'long','semaphore_test':'long','socket_ipv4_udp_unbound_loopback_nogotsan_test':'long'}
seconds={'short':60,'moderate':300,'long':900,'eternal':3600}
contracts={}
for outer in (out/'full-targets').read_text().splitlines():
    owner=outer[:-len('_64k_tcg')];payload=owner+'_64k_arm64'
    family=owner.split(':')[1].split('_runsc_',1)[0]
    assert attribute(outer,'payload')==payload and attribute(outer,'image')=='//test/rbe/tcg:guest'
    attrs={name:attribute(owner,name) for name in ('size','timeout','shard_count','args','flaky')}
    expected=overrides.get(family,attrs['timeout'])
    for name,value in attrs.items():
        assert attribute(payload,name)==value,(payload,name)
        assert attribute(owner+'_rc_kvm',name)==value,(owner,name,'KVM')
        for target in (outer,owner+'_rc_tcg'):
            expected_value = value
            if name == 'timeout':
                expected_value = expected
            elif name == 'shard_count' and family == 'socket_stress_test':
                expected_value = '33'
            elif name == 'args':
                expected_value = ['--strace=false'] + value
            assert attribute(target,name)==expected_value,(target,name)
    contracts[outer]={'owner':owner,'payload':payload,'nativeAttributes':attrs,'tcgTimeout':expected,'seconds':seconds[expected],'shards':33 if family=='socket_stress_test' else max(1,int(attrs['shard_count']))}
assert len(contracts)==661 and sum(row['shards'] for row in contracts.values())==1286
selection = {label: {'shards': row['shards'], 'seconds': row['seconds']} for label, row in contracts.items()}
(out/'selection.json').write_text(json.dumps(selection, indent=2)+'\n')
(out/'contracts.json').write_text(json.dumps(contracts,indent=2)+'\n')
TCG_ATTRIBUTES
        printf '%s\n' "$@" > "$cohort_dir/executed-arguments.txt"
      elif [[ $argument == test && ${QUALIFICATION_RC_BUCKET11_PART:-none} != none ]]; then
        cohort_dir="$RUNNER_TEMP/qualification/rc-bucket11"
        mkdir -p "$cohort_dir"
        printf '%s\n' "$@" > "$cohort_dir/original-arguments.txt"
        remaining=$(python3 -c 'import os,time; print(max(0,int(float(os.environ["QUALIFICATION_WORK_DEADLINE"])-time.monotonic())))')
        # Reserve the original hour plus ten minutes for cached build/setup.
        (( remaining >= 4200 ))
        set -- "$@" --test_arg=--strace=false
        printf '%s\n' "$@" > "$cohort_dir/executed-arguments.txt"
        git status --porcelain --untracked-files=no > "$cohort_dir/source-before.txt"
        [[ ! -s "$cohort_dir/source-before.txt" ]]
        git cat-file -p HEAD > "$cohort_dir/commit.txt"
        local source_file
        for source_file in .bazelrc test/rbe/actions.sh test/rbe/qualify.sh test/rbe/rc_bucket11_unfinished.targets test/rbe/tcg/BUILD test/rbe/tcg/defs.bzl test/rbe/tcg/build_image.sh test/rbe/tcg/init.sh test/rbe/tcg/run.sh test/syscalls/BUILD test/syscalls/linux/mmap_eternal.cc test/runner/defs.bzl test/runner/runner_test.bzl; do
          git show "HEAD:$source_file" > "$cohort_dir/source-${source_file//\//_}.source"
        done
      fi
      if [[ $QUALIFICATION_EXECUTION == remote-actions ]]; then
        remaining=$(python3 -c 'import os,time; print(max(0,int(float(os.environ["QUALIFICATION_WORK_DEADLINE"])-time.monotonic())))')
        if (( remaining > 0 )); then
          timeout --signal=INT --kill-after=30s "${remaining}s" \
            "$(command -v bazelisk)" --bazelrc="$qualification_rc" "$@" "${evidence[@]}" || result=$?
        else
          printf 'Qualification work deadline exhausted.\n' >&2
          result=124
        fi
      else
        remaining=$(python3 -c 'import os,time; print(max(0,int(float(os.environ["QUALIFICATION_WORK_DEADLINE"])-time.monotonic())))')
        (( remaining > 0 ))
        if [[ $qualification_root_bazel == true ]]; then
          timeout --signal=INT --kill-after=30s "${remaining}s" \
            sudo -n -H env "USE_BAZEL_VERSION=$USE_BAZEL_VERSION" \
            "$(command -v bazelisk)" --bazelrc="$qualification_rc" "$@" "${evidence[@]}" || result=$?
        else
          timeout --signal=INT --kill-after=30s "${remaining}s" \
            "$(command -v bazelisk)" --bazelrc="$qualification_rc" "$@" "${evidence[@]}" || result=$?
        fi
      fi
      if [[ -n $raw_events ]]; then
        timeout --signal=TERM --kill-after=5s 600s python3 - "$raw_events" "$events_output" <<'GUEST_EVENTS' || capture_status=$?
import json
from pathlib import Path
import sys

source, target = map(Path, sys.argv[1:])
keys = {"id", "children", "started", "finished", "configured", "completed", "testResult", "testSummary", "aborted", "namedSetOfFiles"}
errors = []
count = 0
with source.open() as stream, target.open("w") as output:
    for line_number, line in enumerate(stream, 1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            errors.append({"line": line_number, "error": str(error)})
            continue
        if "started" in event:
            safe = {"uuid", "startTime", "startTimeMillis", "command"}
            event["started"] = {key: value for key, value in event["started"].items() if key in safe}
        serialized = json.dumps({key: value for key, value in event.items() if key in keys})
        assert "x-buildbuddy-api-key" not in serialized.lower()
        output.write(serialized + "\n")
        count += 1
if count == 0:
    errors.append({"error": "No complete build events"})
target.with_suffix(".errors.json").write_text(json.dumps(errors) + "\n")
raise SystemExit(bool(errors))
GUEST_EVENTS
        rm -f "$raw_events"
        rmdir "$raw_events_dir"
        printf '%s\n' "$result" > "$events_output.bazel-exit"
        printf '%s\n' "$capture_status" > "$events_output.capture-exit"
        if (( result == 0 )); then result=$capture_status; fi
        git rev-parse HEAD > "$cohort_dir/final-head.txt"
        if [[ $(cat "$cohort_dir/final-head.txt") != "$QUALIFICATION_COMMIT" ]]; then result=1; fi
        git status --porcelain --untracked-files=no > "$cohort_dir/source-after.txt"
        if [[ -s "$cohort_dir/source-after.txt" ]]; then result=1; fi
        return "$result"
      fi
      return "$result"
    }
    export -f bazel

    # These are observations of this VM, not claims about other hosted images.
    {
      uname -a
      id
      printf 'page_size=%s\n' "$(getconf PAGESIZE)"
      printf 'logical_cpus=%s\n' "$(getconf _NPROCESSORS_ONLN)"
      awk '$1 == "MemTotal:" { print }' /proc/meminfo
      df -h "$PWD"
      ps -p 1 -o comm=
      stat -fc 'cgroup_filesystem=%T' /sys/fs/cgroup
      sysctl fs.suid_dumpable
      if [[ -d /sys/module/ip6_tables ]]; then
        printf 'ip6_tables sysfs entry before setup: present\n'
      else
        printf 'ip6_tables sysfs entry before setup: absent\n'
      fi
      for device in /dev/kvm /dev/vhost-net /dev/net/tun; do
        if [[ -c $device ]]; then
          ls -l "$device"
        else
          printf '%s: unavailable\n' "$device"
        fi
      done
      sysctl kernel.unprivileged_userns_clone kernel.apparmor_restrict_unprivileged_userns || true
      systemctl --version
      docker version || true
    } | tee "$RUNNER_TEMP/qualification/host.txt"
    if [[ $QUALIFICATION_EXECUTION == local ]]; then
      # The unchanged release smoke deliberately exercises unprivileged setup.
      [[ $(id -u) != 0 ]]
      [[ $(getconf PAGESIZE) == 4096 ]]
      sudo -n true
      # Match the Buildkite test-host setup on this ephemeral Actions VM.
      # Ubuntu's restriction prevents the rootless runtime's user namespace.
      if [[ $(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null) == 1 ]]; then
        sudo -n sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
        [[ $(sysctl -n kernel.apparmor_restrict_unprivileged_userns) == 0 ]]
      fi
      if [[ ${lanes[*]} == syscalls || ${lanes[*]} == syscalls-resume || ${lanes[*]} == syscalls-kvm ]]; then
        # The maintained rtnetlink syscall owners invoke ip and OpenBSD nc.
        sudo -n apt-get update
        sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y iproute2 netcat-openbsd
        dpkg-query -W iproute2 netcat-openbsd | tee "$RUNNER_TEMP/qualification/network-tools.txt"
        command -v ip nc
      fi
      if [[ ${lanes[*]} == syscalls && $QUALIFICATION_ARCH == amd64 ]]; then
        # The AMD64 profile includes native IPv6 netfilter tests; their raw
        # sockopts do not load the legacy handler.
        sudo -n modprobe ip6_tables
      fi
      if [[ ${lanes[*]} == nftables || ${lanes[*]} == moby ]]; then
        # These suites exercise nftables in their private Docker namespaces.
        sudo -n modprobe nfnetlink
        sudo -n modprobe nf_tables
      fi
      if [[ ${lanes[*]} == plugin-network ]]; then
        # The plugin opens host vhost-net and TUN devices inside its sandbox.
        for device in /dev/vhost-net /dev/net/tun; do
          [[ -c $device ]]
          sudo -n test -r "$device"
          sudo -n test -w "$device"
        done
      fi
      if [[ ${lanes[*]} == kvm || ${lanes[*]} == syscalls-kvm || ${lanes[*]} == syscalls-rc-pilot || ${lanes[*]} == syscalls-rc ]]; then
        [[ -c /dev/kvm ]]
        sudo -n test -r /dev/kvm
        sudo -n test -w /dev/kvm
      fi
    fi
    ;;
  *) printf 'Unknown qualification execution mode.\n' >&2; exit 2 ;;
esac

if [[ -n $QUALIFICATION_HEADER_BASE ]]; then
  if [[ ! $QUALIFICATION_HEADER_BASE =~ ^[0-9a-f]{40}$ ]]; then
    printf 'Supply the full header comparison commit SHA.\n' >&2
    exit 2
  fi
  # The remote checkout is separate from Actions and may be shallow. Fetch both
  # histories so qualify.sh can determine the exact added-file selection.
  fetch_options=()
  if [[ $(git rev-parse --is-shallow-repository) == true ]]; then
    fetch_options+=(--unshallow)
  fi
  git fetch "${fetch_options[@]}" origin "$QUALIFICATION_COMMIT" "$QUALIFICATION_HEADER_BASE"
  options+=("--header-base=$QUALIFICATION_HEADER_BASE")
fi

if [[ -n $QUALIFICATION_COS_GZIP_BASE64 || -n $QUALIFICATION_COS_SHA256 ]]; then
  if [[ -z $QUALIFICATION_COS_GZIP_BASE64 || ! $QUALIFICATION_COS_SHA256 =~ ^[0-9a-f]{64}$ ]]; then
    printf 'Supply both a gzip/base64 COS catalog and its uncompressed SHA256.\n' >&2
    exit 2
  fi
  COS_IMAGES_JSON=$(mktemp)
  export COS_IMAGES_JSON
  temporary_files+=("$COS_IMAGES_JSON")
  printf '%s' "$QUALIFICATION_COS_GZIP_BASE64" | base64 --decode | gzip --decompress > "$COS_IMAGES_JSON"
  printf '%s  %s\n' "$QUALIFICATION_COS_SHA256" "$COS_IMAGES_JSON" | sha256sum --check --strict
fi

test/rbe/qualify.sh "${options[@]}" "${lanes[@]}"
