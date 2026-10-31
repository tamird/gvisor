#!/bin/bash
set -euo pipefail
dir=$1
bin=$2
cpu=$(python3 -c 'import os; print(min(os.sched_getaffinity(0)))')
export GOMAXPROCS=1 GO_TEST_WRAP=0
flags=(-test-checkpoint=true -runtime=runsc "-config_path=$dir/daemon.json" -test.cpu=1)
taskset -c "$cpu" "$bin" -test.v -test.run='^$' -test.bench='^BenchmarkCheckpointFile$/^EncodeStatefile$' -test.benchtime=1x "${flags[@]}" > "$dir/bench.log" 2>&1 || { tail -70 "$dir/bench.log"; exit 1; }
grep -E 'WINDOW-KERNEL|BenchmarkCheckpointFile|encoded-kernel-B|Checkpoint file sizes|^PASS' "$dir/bench.log"
grep -Eq 'WINDOW-KERNEL root=\*kernel[.]Kernel attempts=[0-9]+ extensions=[0-9]+ bytes=[1-9][0-9]+' "$dir/bench.log"
grep -Fq 'BenchmarkCheckpointFile/EncodeStatefile' "$dir/bench.log"
grep -Eq '(^|[[:space:]])1[[:space:]]+[[:digit:].]+ ns/op.*encoded-kernel-B' "$dir/bench.log"
grep -qx PASS "$dir/bench.log"
