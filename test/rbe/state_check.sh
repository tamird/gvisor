set -euo pipefail
out="$RUNNER_TEMP/graph-results"
cpu=$(python3 -c 'import os; print(min(os.sched_getaffinity(0)))')
export GOMAXPROCS=1
uname -srmo >"$out/native"
lscpu | grep -E '^(Model name|Architecture):' >>"$out/native"
printf 'pinned CPU: %s; GOMAXPROCS=1\n' "$cpu" >>"$out/native"
go version >>"$out/native"
target=//pkg/state/tests:tests_test
common=(--config=x86_64 -c opt --jobs=12 --remote_download_outputs=toplevel)
baz=(bazel --bazelrc="$rc")
prune(){ grep -E '^(ERROR:|FAIL:|INFO: Streaming|[^ ]+\.go:[0-9]+:)' "$1" | head -24 || true; }
profile_arm() (
 set -euo pipefail
 arm=$1 src=$2 dir="$out/$1"
 mkdir -p "$dir"; git checkout --quiet --detach "$src"
 test "$(git rev-parse HEAD)" = "$src"
 git rev-parse HEAD >"$dir/head"
 git status --porcelain --untracked-files=no >"$dir/before"; test ! -s "$dir/before"
 "${baz[@]}" build "${common[@]}" "$target" >"$RUNNER_TEMP/$arm-build.log" 2>&1 || { prune "$RUNNER_TEMP/$arm-build.log"; exit 1; }
 prune "$RUNNER_TEMP/$arm-build.log" >"$dir/build-summary"
 "${baz[@]}" cquery "${common[@]}" --output=files "$target" >"$dir/files" 2>"$RUNNER_TEMP/$arm-cquery.log"
 mapfile -t bins < <(while IFS= read -r path; do if [[ -f $path && -x $path ]]; then echo "$path"; fi; done <"$dir/files")
 [[ ${#bins[@]} == 1 ]]
 bin="$PWD/${bins[0]}"; sha256sum "$bin" | cut -d' ' -f1 >"$dir/bin.sha256"
 args=(-test.run='^$' -test.bench='^BenchmarkGraphSave$' -test.count=1 -test.benchmem)
 if [[ $arm == probe ]]; then
  taskset -c "$cpu" "$bin" "${args[@]}" -test.benchtime=5x >"$dir/counts.log"
 else
  taskset -c "$cpu" "$bin" "${args[@]}" -test.benchtime=40x "-test.cpuprofile=$dir/cpu.pb" >"$dir/cpu.log"
  taskset -c "$cpu" "$bin" "${args[@]}" -test.benchtime=2x "-test.memprofile=$dir/alloc.pb" -test.memprofilerate=1 >"$dir/alloc.log"
  for profile in cpu alloc; do
   pf=(-nodecount=60 -nodefraction=0 -edgefraction=0)
   if [[ $profile == alloc ]]; then pf+=( -sample_index=alloc_objects ); fi
   go tool pprof "${pf[@]}" -top "$bin" "$dir/$profile.pb" >"$dir/$profile-top.txt"
   go tool pprof "${pf[@]}" -top -cum "$bin" "$dir/$profile.pb" >"$dir/$profile-cumulative.txt"
   go tool pprof "${pf[@]}" '-list=graphEncodeState\)\.(index|query|wideIndex|release)$' "$bin" "$dir/$profile.pb" >"$dir/$profile-lines.txt" 2>&1 || true
  done
 fi
 git status --porcelain --untracked-files=no >"$dir/after"; test ! -s "$dir/after"
)
set +e
profile_arm control 6e1c4aad76d023a5313f00625604f01b5705dc8b; s1=$?
profile_arm candidate dae978bc7dfa7bb7f981295b9b080a8320dd9cf4; s2=$?
profile_arm probe 4c8bd9e42022e7364e48558b0bb8ace79092b8a0; s3=$?
set -e
printf '%s\n' "$s1" "$s2" "$s3" >"$out/returncodes"
python3 - "$out" <<'PYPROBE'
import json,re,sys
from pathlib import Path
p=Path(sys.argv[1]); errors=[]
def need(ok, why):
 if not ok:errors.append(why)
try:
 need((p/'returncodes').read_text().split()==['0']*3,'an arm failed')
 for a,sha in [('control','6e1c4aad76d023a5313f00625604f01b5705dc8b'),('candidate','dae978bc7dfa7bb7f981295b9b080a8320dd9cf4'),('probe','4c8bd9e42022e7364e48558b0bb8ace79092b8a0')]:
  d=p/a
  need((d/'head').read_text().strip()==sha,'wrong source '+a)
  need(not (d/'before').read_text() and not (d/'after').read_text(),'dirty '+a)
  for f,it in ([('counts.log',5)] if a=='probe' else [('cpu.log',40),('alloc.log',2)]):
   log=(d/f).read_text()
   lines=[l for l in log.splitlines() if l.startswith('BenchmarkGraphSave') or re.search(r'^\s+'+str(it)+r'\s',l)]
   need(any(re.search(r'\s'+str(it)+r'\s',l) and re.search(r'\s9454794\s+encoded-B',l) for l in lines) and 'PASS' in log,'bad benchmark record '+a+'/'+f)
  if a!='probe':
   for typ in ('cpu','alloc'):
    need((d/(typ+'.pb')).stat().st_size>500 and len((d/(typ+'-cumulative.txt')).read_text().strip())>250,'bad profile '+a+'/'+typ)
 rows=[json.loads(s.split('GRAPH_INDEX_COUNTS ',1)[1]) for s in (p/'probe/counts.log').read_text().splitlines() if 'GRAPH_INDEX_COUNTS ' in s]
 need(len(rows)==5,'wrong number of graph counts: '+str(len(rows)))
 for r in rows:
  need(r['TotalQueries']==sum(r['QueryWidthLog2']) and r['WideObjectsRegistered'] and r['SmallObjectsRegistered'] and r['SmallKeysAdded'] and r['WideQueries'] and r['NarrowQueriesOfWideTree'],'missing operations')
 (p/'counts.json').write_text(json.dumps(rows,indent=2)+'\n')
except Exception as e: errors.append(str(e))
v={'status':'FAIL' if errors else 'PASS','errors':errors}
(p/'pair-verdict.json').write_text(json.dumps(v)+'\n')
print(v);sys.exit(bool(errors))
PYPROBE
