#!/bin/bash
set -euo pipefail
dir=$1
stock=$2
candidate=$3
cpu=$(python3 -c 'import os; print(min(os.sched_getaffinity(0)))')
export GOMAXPROCS=1 GO_TEST_WRAP=0
export STREAM_DIR="$dir/archive-study"
mkdir "$STREAM_DIR"
flags=(-test.v -test.run='^$' -test.benchtime=1x -test-checkpoint=true -runtime=runsc "-config_path=$dir/daemon.json" -test.cpu=1)
bench() {
  local bin=$1 mode=$2
  taskset -c "$cpu" "$bin" "${flags[@]}" "-test.bench=^Benchmark${mode}$" > "$dir/trial.log" 2>&1 || { tail -38 "$dir/trial.log"; return 1; }
  grep -qx PASS "$dir/trial.log"
  grep -Eo '(STREAM-CAPTURE|STUDY-(DONE|PHASE|PROFILE)) .*' "$dir/trial.log"
}
bench "$candidate" StreamCapture
for spec in phase-cpu:24 phase-alloc:8 sample:160 heap:2; do
 mode=${spec%:*};n=${spec#*:}
 for ((i=0;i<n;i++));do
   if [[ $mode == heap ]];then GODEBUG=memprofilerate=1 STUDY_MODE=$mode STUDY_ID=$i bench "$candidate" ColdDecode >> "$dir/profile.log";else STUDY_MODE=$mode STUDY_ID=$i bench "$candidate" ColdDecode >> "$dir/profile.log";fi || { tail -15 "$dir/profile.log";exit 1; }
 done
done
python3 - "$dir/profile.log" <<'EOF'
from collections import Counter, defaultdict
import json,statistics,sys
from pathlib import Path
phases=defaultdict(list);counts=Counter();pids=set();digests=set();profiles=defaultdict(Counter);done=set();phaseids=defaultdict(set)
for line in Path(sys.argv[1]).read_text().splitlines():
 if line.startswith('STUDY-DONE '):
  v=dict(x.split('=') for x in line.split()[1:]);key=(v['mode'],v['id']);assert key not in done;done.add(key);counts[key[0]]+=1;pids.add(v['pid']);digests.add(v['digest'])
 if line.startswith('STUDY-PHASE '):
  v=dict(x.split('=') for x in line.split()[1:]);key=(v['mode'],v['id'],v['root']);phaseids[key].add(v['phase']);phases[v['mode']+'/'+v['root']+'/'+v['phase']].append([int(v[k]) for k in ['cpu','wall','allocs','bytes']]);
 if line.startswith('STUDY-PROFILE '):
  kind,i,raw=line.split(' ',3)[1:];v=json.loads(raw)
  for group,cols in v.items():profiles[kind+'/'+group].update(cols)
expected={'input_copy','schema','types','objects','targets','decode','callbacks','release'};complete=len(phaseids)==64 and all(s==expected for s in phaseids.values()); result={'counts':counts,'pids':len(pids),'digests':sorted(digests),'phase_complete':complete,'phase_medians':{k:list(map(statistics.median,zip(*rows))) for k,rows in phases.items()},'profiles':{k:c.most_common(24) for k,c in profiles.items()},'sample_ns':{g:profiles['sample/'+g+'/total']['all'] for g in ('cpuid','kernel')},'heap_bytes':profiles['heap/kernel/total']['all']};print('STUDY-RESULT '+json.dumps(result,separators=(',',':')))

EOF
