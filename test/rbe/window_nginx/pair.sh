#!/bin/bash
set -euo pipefail
dir=$1
bin=$2
cpu=$(python3 -c 'import os; print(min(os.sched_getaffinity(0)))')
export GOMAXPROCS=1 GO_TEST_WRAP=0 STREAM_DIR="$dir/restore-clear"
mkdir "$STREAM_DIR"
flags=(-test.v -test.run='^$' -test.benchtime=1x -test-checkpoint=true -runtime=runsc "-config_path=$dir/daemon.json" -test.cpu=1)
bench() {
  taskset -c "$cpu" "$bin" "${flags[@]}" "-test.bench=^Benchmark${1}$" > "$dir/trial.log" 2>&1 || { tail -38 "$dir/trial.log"; return 1; }
  grep -qx PASS "$dir/trial.log"
  grep -Eo '(RESTORE-CLEAR|STREAM-CAPTURE) .*' "$dir/trial.log"
}
bench StreamCapture
for i in $(seq 0 15); do
  if (( i % 2 == 0 )); then modes=(capacity used); else modes=(used capacity); fi
  for mode in "${modes[@]}"; do
    printf 'CLEAR-TRIAL pair=%d mode=%s\n' "$i" "$mode"
    CLEAR_MODE="$mode" bench RestoreClear
  done
done
printf 'CLEAR-DONE pairs=16 cpu=%s\n' "$cpu"
