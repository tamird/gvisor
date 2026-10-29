#!/bin/bash
# The daemon, image and bundle mount are initialized by the existing fixture.
set -euo pipefail
work=$1
before=$2
after=$3
out=$TEST_UNDECLARED_OUTPUTS_DIR
mkdir -p "$work/shared"
cpu=$(python3 -c 'import os; print(min(os.sched_getaffinity(0)))')
export GOMAXPROCS=1 GO_TEST_WRAP=0
flags=(-test-checkpoint=true -runtime=runsc "-config_path=$work/daemon.json" -test.cpu=1)
export CHECKPOINT_CAPTURE_DIR="$work/shared"
taskset -c "$cpu" "$before" -test.run='^$' -test.bench='^BenchmarkCheckpointFile$/^EncodeStatefile$' -test.benchtime=1x "${flags[@]}" >"$work/capture.log" 2>&1 || { tail -35 "$work/capture.log"; exit 1; }
unset CHECKPOINT_CAPTURE_DIR
test -s "$work/shared/checkpoint.img"; test -s "$work/shared/spec.json"
echo "capture: four-worker nginx supplied by checkpointNginx"
(cd "$work/shared"; sha256sum *.img spec.json) >"$out/fixture.sha256"
sha256sum "$before" "$after" >"$out/binaries.sha256"
flags+=("-checkpoint-dir=$work/shared")
for mode in before after; do
  bin=$before; [[ $mode == before ]] || bin=$after
  taskset -c "$cpu" "$bin" -test.v -test.run='^TestCheckpointFileRoundTrip$' "${flags[@]}" >"$work/roundtrip-$mode.log" 2>&1 || { tail -40 "$work/roundtrip-$mode.log"; exit 1; }
  grep -q -- '--- PASS: TestCheckpointFileRoundTrip' "$work/roundtrip-$mode.log"
  grep 'State file sizes:' "$work/roundtrip-$mode.log"
done
for pair in $(seq 0 16); do
  order='before after'
  if (( pair % 2 == 0 )); then order='after before'; fi
  for mode in $order; do
    bin=$before; [[ $mode == before ]] || bin=$after
    log=$work/$pair-$mode.log
    taskset -c "$cpu" "$bin" -test.run='^$' -test.bench='^BenchmarkCheckpointFile$/^(DecodeStatefile|EncodeStatefile)$' -test.benchtime=1x -test.benchmem "${flags[@]}" >"$log" 2>&1 || { tail -30 "$log"; exit 1; }
    test "$(grep -c '^BenchmarkCheckpointFile/' "$log")" -eq 2
    grep -Eq '^BenchmarkCheckpointFile/EncodeStatefile(-[0-9]+)?[[:space:]]+1[[:space:]].*[1-9][0-9]* encoded-state-file-B' "$log"
    grep -Eq '^BenchmarkCheckpointFile/DecodeStatefile(-[0-9]+)?[[:space:]]+1[[:space:]].*[1-9][0-9]* state-file-B' "$log"
    if (( pair )); then
      grep '^BenchmarkCheckpointFile/' "$log" >> "$out/$mode.bench"
      echo "$pair $mode" >> "$out/order.txt"
    fi
  done
done
(cd "$work/shared"; sha256sum -c "$out/fixture.sha256")
test "$(wc -l < "$out/before.bench")" -eq 32
test "$(wc -l < "$out/after.bench")" -eq 32
echo "CHECKPOINT_PAIR_COMPLETE 16 AB/BA pairs, 2 benchmarks, 1 operation per sample"
{
  printf '<testsuite name="shared-checkpoint" tests="18">\n'
  printf '<testcase name="before-three-roundtrips"/><testcase name="after-three-roundtrips"/>\n'
  for n in $(seq 1 16); do printf '<testcase name="matched-encode-decode-pair-%d"/>\n' "$n"; done
  echo '</testsuite>'
} > "$XML_OUTPUT_FILE"
