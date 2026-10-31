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
  taskset -c "$cpu" "$bin" "${flags[@]}" "-test.bench=^BenchmarkStream${mode}$" > "$dir/trial.log" 2>&1 || { tail -38 "$dir/trial.log"; return 1; }
  grep -qx PASS "$dir/trial.log"
  grep -E '^STREAM-(ARCHIVE|PEER|CAPTURE) ' "$dir/trial.log"
}
bench "$candidate" Capture
for i in $(seq 0 15); do
 if (( i % 2 == 0 )); then arms=(stock candidate); else arms=(candidate stock); fi
 for arm in "${arms[@]}"; do
   printf 'STREAM-TRIAL pair=%d arm=%s\n' "$i" "$arm"
   bench "${!arm}" Archive
   if (( i == 0 )); then
     if [[ $arm == stock ]]; then peer=candidate; else peer=stock; fi
     printf 'STREAM-CROSS producer=%s reader=%s\n' "$arm" "$peer"
     bench "${!peer}" PeerArchive
   fi
 done
done
printf 'STREAM-DONE pairs=16 captures=1 cross=2 worker=%s\n' "$cpu"
