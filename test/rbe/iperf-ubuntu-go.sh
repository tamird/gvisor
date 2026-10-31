set -euo pipefail
name=$1; arm=$2
case "$arm" in x86_64) cpu=amd64;; aarch64) cpu=arm64;; *) exit 2;; esac
r="$RUNNER_TEMP/iperf-results/$name-go-$arm"; mkdir -p "$r"; exec > >(tee "$r/coordinator.log") 2>&1
bz() { bazel --bazelrc="$RUNNER_TEMP/images.bazelrc" "$@"; }
work=$(mktemp -d "$RUNNER_TEMP/bench-go-XXXX"); trap 'rm -rf "$work"' EXIT
if [[ "$name" == hackbench ]]; then
  product=4be56fbed6ca78d3574908bb6ece9072dc1c040f; target=//test/benchmarks/base:hackbench_test; bench='^BenchmarkHackbench$'
  git checkout -q --detach "$product"; label=//images/benchmarks/hackbench:hackbench_context
  bz build --experimental_remote_require_cached --config="$arm" "$label"
  archive=$(bz cquery --config="$arm" --output=files "$label")
  member=$(tar -tf "$archive" | grep -Ex '(\./)?images/benchmarks/hackbench/image.tar')
  tar -xOf "$archive" "$member" >"$work/image.tar"; docker load --input "$work/image.tar"; image=gvisor.dev/images/benchmarks/hackbench:latest
else
  product=05f0c6feac4693941c8ccf6b8730ddf3c258a51c; target=//test/benchmarks/network:iperf_test; bench='^BenchmarkIperf'
  git checkout -q --detach "$product"; image=gvisor.dev/images/benchmarks/iperf:latest
  docker tag "gvisor.dev/images/benchmarks/iperf:$cpu" "$image"
fi
git rev-parse HEAD > "$r/product"; grep -Fx "$product" "$r/product"; test "$(docker image inspect -f '{{.Architecture}}' "$image")" = "$cpu"
ret=0
bz test --config="$arm" --jobs=2 '--strategy_regexp=Compiling src/google/protobuf/.*\.cc=linux-sandbox' --strategy=TestRunner=local --nocache_test_results --test_output=errors --test_arg=-test.v '--test_arg=-test.run=^$' --test_arg="-test.bench=$bench" --test_arg=-test.benchtime=1x --test_arg=-runtime=runc --test_timeout=600 "$target" || ret=$?
d="bazel-testlogs/${target:2}";d=${d/:/\/}
[[ ! -f "$d/test.log" ]] || cp "$d/test.log" "$r/test.log"
[[ ! -f "$d/test.xml" ]] || cp "$d/test.xml" "$r/test.xml"
[[ "$ret" == 0 && -f "$r/test.xml" ]]
python3 - "$r" "$name" <<'PY'
from pathlib import Path
import sys
r=Path(sys.argv[1]);s=(r/'test.log').read_text()
assert '--- SKIP:' not in s and '--- FAIL:' not in s
if sys.argv[2]=='hackbench':
    cases=['BenchmarkHackbench/ipcMode.pipe/processMode.thread','BenchmarkHackbench/ipcMode.socket/processMode.process'];metric='execution_time_per_loop'
else:
    cases=[f'BenchmarkIperfOneConnection/operation.{direction}' for direction in ['Upload','Download']]
    cases += [f'BenchmarkIperfManyConnections/operation.{direction}/parallel.{count}' for count in [4,16,64] for direction in ['Upload','Download']];metric='bandwidth'
rows=[]
for case in cases:
    matches=[s for s in s.splitlines() if case in s and metric in s]
    assert len(matches)==1,(case,s[-4000:]);rows.extend(matches)
(r/'benchmark-results').write_text('\n'.join(rows)+'\n')
print(*rows,sep='\n')
PY
echo PASS >"$r/verdict"
