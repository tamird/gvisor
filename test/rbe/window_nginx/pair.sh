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
  grep -Eo '(STREAM-CAPTURE|COLD-(TRIAL|HTTP)) .*' "$dir/trial.log"
}
bench "$candidate" StreamCapture
for i in $(seq 0 63);do
 if (( i % 2 == 0 )); then arms=(stock candidate);else arms=(candidate stock);fi
 for arm in "${arms[@]}"; do
   echo "COLD-ARM pair=$i arm=$arm";bench "${!arm}" ColdDecode
 done
done
echo 'COLD-SAMPLES pairs=64'
rc=0
for i in $(seq 1 3);do
   echo "COLD-LIVE $i";COLD_LIVE=1 bench "$candidate" StreamCapture || rc=1
done
python3 - "$dir/trace" <<'EOF'
from pathlib import Path
import sys,re
n=0
for p in sorted(Path(sys.argv[1]).rglob('*')):
 if not p.is_file():continue
 matches=re.findall(r'COLD-RESTORE-POOL [^\n]*',p.read_text(errors='replace'))
 if not matches:continue
 n+=1;ids={}
 for entry in matches:
  line=re.sub(r'decoder=(0x[0-9a-f]+)',lambda m:'decoder='+str(ids.setdefault(m[1],len(ids)+1)),entry);print(f'COLD-LOG group={n} '+line)
EOF
exit "$rc"
