#!/bin/bash
set -euo pipefail
dir=$1
bench=$3
cpu=$(python3 -c 'import os;print(min(os.sched_getaffinity(0)))')
export GOMAXPROCS=1 GO_TEST_WRAP=0
flags=(-test.v -test.run='^$' -test.benchtime=1x -test-checkpoint=true -runtime=runsc "-config_path=$dir/daemon.json" -test.cpu=1 '-test.bench=^BenchmarkLiveService$')
all=(stock_copy candidate_runfiles stock_runfiles candidate_copy)
for arm in "${all[@]}"; do
  p=$dir/$arm
  a=$(sha256sum "$p/runsc" | cut -d' ' -f1)
  b=$(sha256sum "$p/gvisor-bin/gvisor_sentry" | cut -d' ' -f1)
  tree=$(cd "$p" && sha256sum runsc containerd-shim-runsc-v1 gvisor-bin/* | sha256sum | cut -d' ' -f1)
  fs=$(stat -f -c %T "$p/gvisor-bin/gvisor_sentry")
  echo "SERVICE-IDENT arm=$arm runsc=$a sentry=$b tree=$tree fs=$fs"
done
for i in $(seq -1 3);do
  case $i in
    -1|0) arms=(stock_copy candidate_runfiles stock_runfiles candidate_copy);;
    1) arms=(candidate_runfiles candidate_copy stock_copy stock_runfiles);;
    2) arms=(candidate_copy stock_runfiles candidate_runfiles stock_copy);;
    3) arms=(stock_runfiles stock_copy candidate_copy candidate_runfiles);;
  esac
  for arm in "${arms[@]}";do
    echo "SERVICE-ARM pair=$i arm=$arm"
    ln -sfn "$dir/$arm" "$dir/current"
    taskset -c "$cpu" "$bench" "${flags[@]}" > "$dir/bench.log" 2>&1 || { tail -35 "$dir/bench.log";exit 1; }
    grep -qx PASS "$dir/bench.log"
    grep -Eo 'SERVICE-TRIAL .*' "$dir/bench.log"
    python3 - "$dir/trace" <<'PY'
from pathlib import Path
import re,sys
hits={'CPUID':[],'Kernel':[]}
for p in Path(sys.argv[1]).rglob('*'):
  if not p.is_file():continue
  for what,count,unit in re.findall(r'(CPUID|Kernel) load took \[([.\d]+)(ns|µs|us|ms|s)\]',p.read_text(errors='replace')):
    hits[what].append(round(float(count)*{'ns':1,'µs':1000,'us':1000,'ms':1000000,'s':1000000000}[unit]))
  p.unlink()
if len(hits['CPUID'])!=1 or len(hits['Kernel'])!=1:raise ValueError(hits)
print(f"SERVICE-KERNEL cpuid={hits['CPUID'][0]} kernel={hits['Kernel'][0]}")
PY
  done
done
echo 'SERVICE-END blocks=4 warmups=4'
