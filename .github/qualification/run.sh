#!/bin/bash
set -euo pipefail
if test "${STRUCTURED_FUNCTIONAL_DEADLINE:-}" != 1;then
 exec timeout --signal=TERM --kill-after=120s 55m env STRUCTURED_FUNCTIONAL_DEADLINE=1 bash "${BASH_SOURCE[0]}"
fi
: "${SOURCE:?}" "${BASE:?}" "${QUAL_ARCH:?}" "${QUAL_CONFIG:?}" "${RESULT_DIR:?}" "${RUNNER_TEMP:?}"
test "$SOURCE" = 5d3c5b90a3be5a7d485f5b70240ae09dd0e00b9c
test "$BASE" = 4ca1ad5d5b713613bc6b8c6f42b0b91720bd97cb
test "$(git rev-parse HEAD)" = "$SOURCE"
test "$QUAL_ARCH:$QUAL_CONFIG:$(uname -m)" = amd64:x86_64:x86_64
test "$(bazel --version)" = 'bazel 9.3.1'
support=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
w="$RUNNER_TEMP/structured-functional"
results="$RESULT_DIR/structured-functional"
mkdir -m 700 "$w" "$results"
stage_pid=
stage_launching=0
cleanup() {
 local rc=$?
 trap - EXIT
 trap '' TERM INT
 if test "$stage_launching" = 1 && test -z "$stage_pid";then exit 1;fi
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
 assert time.monotonic()<deadline
 time.sleep(.05)
WAIT
  then printf '%s\n' stage-quiescence-unproved > "$results/cleanup-incomplete.txt";exit 1;fi
  wait "$stage_pid" 2>/dev/null || true
 fi
 test ! -e "$results/stage-active-group.json" || exit 1
 test ! -e "$w/preflight-active-group.json" || exit 1
 python3 "$support/stage.py" owned-cleanup 20s "$results" python3 "$support/cleanup.py" "$results" || rc=1
 test ! -e "$results/stage-active-group.json" || exit 1
 timeout --kill-after=5s 15s bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc shutdown || rc=1
 if test -f "$results/apparmor-original.txt";then
  read -r original < "$results/apparmor-original.txt"
  timeout --kill-after=1s 4s sudo sysctl -w "kernel.apparmor_restrict_unprivileged_userns=$original" > "$results/apparmor-restored.txt" || rc=1
 fi
 timeout --kill-after=1s 5s git diff --binary > "$results/final-source.diff" || rc=1
 timeout --kill-after=1s 5s python3 - "$results" <<'SOURCE_OUTPUTS' || rc=1
import shutil,sys
from pathlib import Path
output=Path(sys.argv[1])/'final-dependency-files';output.mkdir()
for name in ('MODULE.bazel','go.mod','go.sum'):
 path=Path(name)
 assert path.is_file() and not path.is_symlink(),name
 shutil.copy2(path,output/name)
SOURCE_OUTPUTS
 timeout --kill-after=1s 5s git diff --quiet || rc=1
 timeout --kill-after=1s 5s git diff --cached --quiet || rc=1
 printf '%s\n' "$rc" > "$results/final-status.txt"
 exit "$rc"
}
trap cleanup EXIT
trap 'exit 124' TERM
trap 'exit 130' INT
stage() {
 local label="$1" cap="$2" rc=0
 shift 2
 printf '%q ' "$@" >> "$results/commands.txt";printf '\n' >> "$results/commands.txt"
 stage_launching=1
 python3 "$support/stage.py" "$label" "$cap" "$results" "$@" &
 stage_pid=$!
 stage_launching=0
 wait "$stage_pid" || rc=$?
 stage_pid=
 timeout --kill-after=1s 5s cat "$results/$label.log" || rc=1
 printf '%s\t%s\n' "$label" "$rc" >> "$results/status.tsv"
 test ! -e "$results/stage-active-group.json" || exit 1
 return "$rc"
}
sudo -n true
test -z "$(timeout --kill-after=1s 5s sudo docker ps -aq)"
stage group-control 15s python3 "$support/group_preflight.py" "$w" "$results"
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns;then
 cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns > "$results/apparmor-original.txt"
 timeout --kill-after=1s 5s sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 > "$results/apparmor-active.txt"
fi
bazel_cmd=(bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc)
flags=(--config=x86_64 -c opt --jobs=3 --local_test_jobs=2 --nostamp --remote_executor= --remote_cache= --bes_backend=)
make_args=(DOCKER_BUILD=false 'BAZEL=bazel --host_jvm_args=-Xmx4g --nosystem_rc --nohome_rc' "BAZEL_OPTIONS=${flags[*]}")
stage bind-source 30s python3 "$support/capture.py" bind "$support/expected.json" "$results" "$SOURCE"
status=0
python3 -c 'import time;print(time.time_ns()//1000000)' > "$results/tools-start-ms.txt"
stage tools-tests 10m "${bazel_cmd[@]}" test "${flags[@]}" --nocache_test_results --flaky_test_attempts=1 --test_sharding_strategy=disabled --test_timeout=600 --test_output=all --test_env=GO_TEST_WRAP_TESTV=1 "--build_event_json_file=$results/tools.bep" //test/benchmarks/tools:tools_test || status=1
timeout --kill-after=5s 1m "${bazel_cmd[@]}" info "${flags[@]}" bazel-testlogs > "$results/testlogs-path.txt"
stage exact-tools-xml 30s python3 "$support/capture.py" xml "$support/expected.json" "$results" "$(cat "$results/testlogs-path.txt")" || status=1
test "$status" = 0
stage source-lint 10m make "${make_args[@]}" lint
stage five-consumer-builds 15m "${bazel_cmd[@]}" build "${flags[@]}" //test/benchmarks/network:iperf_test //test/benchmarks/network:nginx_test //test/benchmarks/network:httpd_test //test/benchmarks/network:node_test //test/benchmarks/network:ruby_test //test/gpu/stablediffusion:stablediffusion
for image in hey httpd util;do
 case "$image" in hey) cap=5m;;*) cap=3m;;esac
 stage "image-$image" "$cap" make "${make_args[@]}" "load-benchmarks_$image"
 timeout --kill-after=1s 5s sudo docker image inspect --format '{"id":{{json .Id}},"architecture":{{json .Architecture}}}' "gvisor.dev/images/benchmarks/$image:latest" > "$results/image-$image.json"
done
for phase in native-httpd;do
 test -z "$(timeout --kill-after=1s 5s sudo docker ps -aq)"
 python3 - "$support/expected.json" "$results" "$phase" <<'INTENT'
import json,sys,time
from pathlib import Path
x=json.load(open(sys.argv[1]));r=Path(sys.argv[2]);phase=sys.argv[3];path=r/'native-intents.json'
intents=json.loads(path.read_text()) if path.exists() else []
images={name:json.loads((r/('image-'+name+'.json')).read_text()) for name in ('hey','httpd','util')}
assert all(row['architecture']=='amd64' for row in images.values())
prefixes=['/'+leaf.replace('/','-')+'-' for leaf in x['nativeLeaves'][phase]]
allowed={images['hey']['id']:prefixes,images['httpd']['id']:prefixes,images['util']['id']:['/util-']}
intents.append({'source':x['source'],'started':time.time(),'imagePrefixes':allowed})
temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(intents,indent=2)+'\n');temporary.replace(path)
INTENT
 target=//test/benchmarks/network:httpd_test;selector=BenchmarkContinuousHttpd/filesize.10Kb
 stage "$phase" 2m "${bazel_cmd[@]}" run "${flags[@]}" '--run_under=sudo -E' "--build_event_json_file=$results/$phase.bep" "$target" -- -runtime=runc -test.run=NoTests "-test.bench=$selector" -test.benchtime=1x -test.count=1 -test.cpu=4 -test.timeout=90s
 test -z "$(timeout --kill-after=1s 5s sudo docker ps -aq)"
done
for image in hey httpd util;do
 timeout --kill-after=1s 5s sudo docker image inspect --format '{"id":{{json .Id}},"architecture":{{json .Architecture}}}' "gvisor.dev/images/benchmarks/$image:latest" > "$results/image-$image-final.json"
 cmp "$results/image-$image.json" "$results/image-$image-final.json"
done
stage native-reporter-proof 30s python3 "$support/capture.py" native "$support/expected.json" "$results"
