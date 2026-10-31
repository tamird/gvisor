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
  grep -Eo '(STREAM-CAPTURE|TABLE-(TRIAL|SIZES|DONE)) .*' "$dir/trial.log"
}
bench "$candidate" StreamCapture
for i in $(seq 0 15); do
 if (( i % 2 == 0 )); then arms=(stock candidate); else arms=(candidate stock); fi
 for arm in "${arms[@]}"; do
   printf 'TABLE-ARM pair=%d arm=%s\n' "$i" "$arm"
   bench "${!arm}" DecodeTableTrial
 done
done
printf 'TABLE-COMPLETE pairs=16 captures=1 worker=%s\n' "$cpu"
