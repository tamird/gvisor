set -euo pipefail
arm=$1
product=4be56fbed6ca78d3574908bb6ece9072dc1c040f
r="$RUNNER_TEMP/hackbench-results/$arm"
mkdir -p "$r"
exec > >(tee "$r/coordinator.log") 2>&1
git checkout -q --detach "$product"
git rev-parse HEAD > "$r/product"
test "$(cat "$r/product")" = "$product"
bz() { bazel --bazelrc="$RUNNER_TEMP/images.bazelrc" "$@"; }
pkg=//images/benchmarks/hackbench
export MAKEFLAGS='DOCKER_BUILD=false'

case "$arm" in
  x86_64) cpu=amd64; producer=63361588-f684-4460-8e86-e888d7a57999 ;;
  aarch64) cpu=arm64; producer=f8103834-99f5-4a15-a0d3-7d6be4b5d8d4 ;;
  *) exit 2 ;;
esac
work=$(mktemp -d "$RUNNER_TEMP/hackbench-retained-XXXX")
trap 'rm -rf "$work"' EXIT
label="$pkg:hackbench_context"
git show "$GITHUB_SHA:test/rbe/recover-image-context.py" > "$work/recover.py"
if python3 "$work/recover.py" "$label" "$producer" "$r" "$work/context.tar"; then
  archive="$work/context.tar"
else
  unset BUILDBUDDY_API_KEY
  bz build --experimental_remote_require_cached --config="$arm" "$label"
  archive=$(bz cquery --config="$arm" --output=files "$label")
  test -f "$archive"
  echo 'Recovered with Bazel; rebuilding disallowed.' >"$r/materialization"
fi
unset BUILDBUDDY_API_KEY
tar -xOf "$archive" './images/benchmarks/hackbench/image.tar' >"$work/image.tar" ||
  tar -xOf "$archive" 'images/benchmarks/hackbench/image.tar' >"$work/image.tar"
docker load --input "$work/image.tar"
tag=gvisor.dev/images/benchmarks/hackbench:latest
test "$(docker image inspect "$tag" --format '{{.Architecture}}')" = "$cpu"
docker run --rm --pull=never --runtime=runc --platform=linux/"$cpu" "$tag" cat /usr/share/doc/hackbench/COPYING > "$r/COPYING"
license=$(bz cquery --output=files @rt_tests//:COPYING)
execroot=$(bz info execution_root)
cmp "$r/COPYING" "$execroot/$license"
sha256sum "$r/COPYING" > "$r/copying.sha256"
# Keep raw consumer outcomes even if its Go/Bazel test cannot start.
id=$(docker run --detach --pull=never --runtime=runc --platform=linux/"$cpu" "$tag" sleep 24h)
for mode in thread process; do
  flags=(--groups=10 --process)
  if [[ "$mode" == thread ]]; then flags=(--pipe --groups=10 --threads); fi
  docker exec "$id" hackbench "${flags[@]}" -l 1 | tee "$r/$mode.log"
  grep -Eq '^Time:[[:space:]]+[0-9]+([.][0-9]+)?' "$r/$mode.log"
done
docker rm --force "$id" >/dev/null
target=//test/benchmarks/base:hackbench_test
res=0
bz test --strategy=TestRunner=local --nocache_test_results --test_output=errors --test_arg=-test.v '--test_arg=-test.run=^$' '--test_arg=-test.bench=^BenchmarkHackbench$' --test_arg=-test.benchtime=1x --test_arg=-runtime=runc --test_timeout=300 "$target" || res=$?
d=bazel-testlogs/test/benchmarks/base/hackbench_test
[[ ! -f "$d/test.log" ]] || cp "$d/test.log" "$r/test.log"
[[ ! -f "$d/test.xml" ]] || cp "$d/test.xml" "$r/test.xml"
[[ "$res" == 0 && -f "$r/test.xml" ]]
python3 - "$r" <<'PYTHON'
from pathlib import Path
import re,sys
p=Path(sys.argv[1]);s=(p/'test.log').read_text()
assert '--- SKIP:' not in s and '--- FAIL:' not in s,s
names=['ipcMode.pipe/processMode.thread','ipcMode.socket/processMode.process']
results=[]
for n in names:
 rows=[line for line in s.splitlines() if 'BenchmarkHackbench/'+n in line and 'execution_time_per_loop' in line]
 assert len(rows)==1,(n,s[-4000:])
 results.extend(rows)
(p/'benchmark-results').write_text('\n'.join(results)+'\n')
print(*results,sep='\n')
PYTHON
echo passed > "$r/verdict"
