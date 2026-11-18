#!/bin/bash
set -euo pipefail
if test "${NIC_INFO_LOCK_DEADLINE:-}" != 1;then
 exec timeout --signal=TERM --kill-after=60s 28m env NIC_INFO_LOCK_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}"
test "$SOURCE" = 97b3315886b5346ef29a1cbe1c8d311844eafa92
test "$BASE" = 9ad3ba91c673248c8e522cbca3c37087375b3b25
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
support=$(cd "$(dirname "$0")" && pwd)
results="$RESULT_DIR/nic-info-lock-current"
mkdir -m 700 "$results"
stack=pkg/tcpip/stack/stack.go
stage_pid=
stage_launching=0
cleanup() {
 local rc=$? stopped=0
 trap - EXIT
 trap '' TERM INT
 if test "$stage_launching" = 1 && test -z "$stage_pid";then
  printf '%s\n' 'Stage launch interrupted before PID publication; no competing cleanup' > "$results/cleanup-incomplete.txt"
  printf '%s\n' 1 > "$results/final-status.txt";exit 1
 fi
 if test -n "$stage_pid";then
  kill -TERM "$stage_pid" 2>/dev/null || true
  if ! timeout --kill-after=1s 19s python3 - "$stage_pid" <<'WAIT'
import sys,time
from pathlib import Path
pid=int(sys.argv[1]);deadline=time.monotonic()+18
while True:
 try:raw=Path(f'/proc/{pid}/stat').read_text()
 except FileNotFoundError:break
 if raw.rsplit(')',1)[1].split()[0]=='Z':break
 assert time.monotonic()<deadline,'Stage wrapper termination unproved'
 time.sleep(.05)
WAIT
  then printf '%s\n' 'Stage wrapper remains; inputs retained' > "$results/cleanup-incomplete.txt";exit 1;fi
  wait "$stage_pid" 2>/dev/null || true
 fi
 if test -e "$results/stage-active-group.json";then
  printf '%s\n' 'Whole stage group absence unproved; no competing cleanup' > "$results/cleanup-incomplete.txt"
  printf '%s\n' 1 > "$results/final-status.txt";exit 1
 fi
 if timeout --kill-after=2s 10s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown;then stopped=1;else rc=1;fi
 if test "$stopped" = 1;then
  timeout --kill-after=1s 4s cp "$results/original-stack.go" "$stack" || rc=1
 else printf '%s\n' 'Server quiescence unproved; fixture inputs retained' > "$results/cleanup-incomplete.txt";fi
 if test -f "$results/apparmor-original.txt";then
  read -r original < "$results/apparmor-original.txt"
  timeout --kill-after=1s 4s sudo sysctl -w "kernel.apparmor_restrict_unprivileged_userns=$original" > "$results/apparmor-restored.txt" || rc=1
 fi
 timeout --kill-after=1s 5s git diff --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --quiet || rc=1
 printf '%s\n' "$rc" > "$results/final-status.txt"
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
stage() {
 local name="$1" cap="$2" rc=0
 shift 2
 printf '%q ' "$@" >> "$results/commands.txt";printf '\n' >> "$results/commands.txt"
 stage_launching=1
 python3 "$support/stage.py" "$name" "$cap" "$results" "$@" &
 stage_pid=$!
 stage_launching=0
 wait "$stage_pid" || rc=$?
 stage_pid=
 timeout --kill-after=1s 4s cat "$results/$name.log" || rc=1
 printf '%s\t%s\n' "$name" "$rc" >> "$results/stages.tsv"
 return "$rc"
}
cp "$stack" "$results/original-stack.go"
python3 - "$support/expected.json" <<'PY'
from pathlib import Path
import hashlib,json,subprocess,sys
x=json.loads(Path(sys.argv[1]).read_text());assert hashlib.sha256(Path('pkg/tcpip/stack/stack.go').read_bytes()).hexdigest()==x['stackGoSHA256']
raw=subprocess.check_output(['git','cat-file','commit',x['source']],timeout=5);headers=raw.split(b'\n\n',1)[0].splitlines();assert [line[7:].decode() for line in headers if line.startswith(b'parent ')]==[x['base']]
assert b'Signed-off-by:' not in raw.split(b'\n\n',1)[1]
subprocess.run(['git','diff','--quiet'],check=True,timeout=5);subprocess.run(['git','diff','--cached','--quiet'],check=True,timeout=5)
PY
test "$(bazel --version)" = 'bazel 9.3.1'
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns;then
 cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns > "$results/apparmor-original.txt"
 stage userns-setup 10s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
flags=(--config=x86_64 --jobs=3 --nostamp --remote_executor= --remote_cache= --bes_backend= --color=no --curses=no)
test_flags=(--nocache_test_results --flaky_test_attempts=1 --test_sharding_strategy=disabled --test_timeout=180 --test_output=all)
target=//pkg/tcpip/stack:stack_nogo
cp "$support/prior-prefix.json" "$results/reused-formatting-source-positive-prefix.json"
stage declared-outputs-preflight 2m "${bazel_cmd[@]}" cquery "${flags[@]}" "${test_flags[@]}" --build_tag_filters= --output=files --output_groups=nogo_findings --universe_scope="$target" "config($target, 05123793430e1820e76a0b7ba54f27cd398af88b7e7e0d2d108e506f0352fea0)"
python3 - "$results/declared-outputs-preflight.log" <<'PY'
from pathlib import Path
import sys
rows=[line for line in Path(sys.argv[1]).read_text().splitlines() if line.startswith('bazel-out/')]
assert len(rows)==2 and {Path(line).name for line in rows}=={'stack_nogo.amd64.findings','stack_nogo.arm64.findings'},rows
PY
run_nogo() {
 local name="$1" cap="$2" state="$3" findings_mode="$4" rc=0 capture=0 began cfg
 began=$(python3 -c 'import time;print(time.time_ns()//1000000)')
 printf '%s\n' "$began" > "$results/$name-start-ms.txt"
 stage "$name" "$cap" "${bazel_cmd[@]}" test "${flags[@]}" "${test_flags[@]}" --build_event_json_file="$results/$name.bep" "$target" || rc=$?
 printf '%s\n' "$rc" > "$results/$name-exit.txt"
 stage "$name-capture" 20s python3 "$support/collect.py" tests "$state" "$results/$name.bep" "$results/$name-evidence" "$support/expected.json" "$began" "$target" || capture=$?
 stage "$name-binding" 10s python3 "$support/collect.py" configuration "$results/$name.bep" "$results/$name-binding.json" "$support/expected.json" "$target"
 cfg=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["configuration"])' "$results/$name-binding.json")
 stage "$name-findings-query" 30s "${bazel_cmd[@]}" cquery "${flags[@]}" "${test_flags[@]}" --build_tag_filters= --output=files --output_groups=nogo_findings --universe_scope="$target" "config($target, $cfg)"
 python3 - "$results/$name-findings-query.log" "$results/$name-findings-paths.txt" <<'PY'
from pathlib import Path
import sys
rows=[line for line in Path(sys.argv[1]).read_text().splitlines() if line.startswith('bazel-out/')]
assert len(rows)==2,rows
Path(sys.argv[2]).write_text('\n'.join(rows)+'\n')
PY
 stage "$name-findings-capture" 10s python3 "$support/collect.py" findings "$findings_mode" "$results/$name-findings-paths.txt" "$results/$name-findings" "$support/expected.json" "$target" "$results/$name-binding.json"
 python3 - "$results/$name-findings" "$findings_mode" "$support/expected.json" "$results/$name-evidence" "$began" <<'PY'
from pathlib import Path
import json,sys
x=json.loads(Path(sys.argv[3]).read_text());mode=sys.argv[2]
for arch in ('amd64','arm64'):
 value=json.loads((Path(sys.argv[1])/(arch+'.findings')).read_text())
 if mode=='old':
  assert isinstance(value,list) and len(value)==1,value
  assert value[0]['Position']['Line']==x['oldCheckerPositiveControl']['line'] and 'must hold s.mu' in value[0]['Message'],value
for p in Path(sys.argv[4]).glob('*/test.xml'):assert p.stat().st_mtime_ns//1_000_000>=int(sys.argv[5])
PY
 if test "$state" = passed;then test "$rc" -eq 0;else test "$rc" -eq 3;fi
 test "$capture" -eq 0
}
run_nogo source-positive 12m passed source
stage stop-before-guarded-overlay 15s "${bazel_cmd[@]}" shutdown
cat "$results/original-stack.go" "$support/guarded-controls.go.txt" > "$stack"
run_nogo guarded-positive 5m passed source
stage stop-before-unlocked-overlay 15s "${bazel_cmd[@]}" shutdown
cat "$results/original-stack.go" "$support/guarded-controls.go.txt" "$support/unlocked-control.go.txt" > "$stack"
run_nogo unlocked-negative 5m failed old
printf '%s\n' 'CurrentAMD+ARMstackNogo and guarded/unlocked annotation controls only; no runtime/networkperformance/C++claim.' > "$results/scope.txt"
