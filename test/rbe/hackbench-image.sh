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
if [[ "$arm" == catalog ]]; then
  bz build //tools/licensecheck/main:licensecheck
  lic=$(bz cquery --output=files //tools/licensecheck/main:licensecheck)
  doc=tools/licensecheck/dependencies.yaml
  cp "$doc" "$RUNNER_TEMP/hackbench-catalog.yaml"
  "$lic" --mode=verify >"$r/verify.log" 2>&1 || { tail -12 "$r/verify.log"; exit 1; }
  for run in 1 2; do
    "$lic" --mode=fetch --yaml="$RUNNER_TEMP/hackbench-catalog.yaml" >"$RUNNER_TEMP/hackbench-fetch$run.log" 2>&1 || { tail -20 "$RUNNER_TEMP/hackbench-fetch$run.log"; exit 1; }
    grep -F 'rt_tests' "$RUNNER_TEMP/hackbench-fetch$run.log" >"$r/fetch$run.log" || true
    cp "$RUNNER_TEMP/hackbench-catalog.yaml" "$RUNNER_TEMP/hackbench-catalog$run.yaml"
  done
  cmp "$RUNNER_TEMP/hackbench-catalog1.yaml" "$RUNNER_TEMP/hackbench-catalog2.yaml"
  "$lic" --mode=verify --yaml="$RUNNER_TEMP/hackbench-catalog.yaml" >"$r/fetched-verify.log" 2>&1 || { tail -12 "$r/fetched-verify.log"; exit 1; }
  python3 - "$doc" "$RUNNER_TEMP/hackbench-catalog.yaml" "$r" <<'PYTHON'
import re,sys
from pathlib import Path
stock,new,out=map(Path,sys.argv[1:])
def rows(p):
    return dict(re.findall(r"(?m)^- dependency: ([^\n]+)\n((?:  [^\n]*\n)+)",p.read_text()))
old,cur=rows(stock),rows(new)
assert cur==old,[k for k in old.keys()|cur.keys() if old.get(k)!=cur.get(k)]
assert 'license: GPL-2.0-only' in cur['rt_tests']
(out/'license.txt').write_text(cur['rt_tests'])
(out/'counts').write_text(f'{len(cur)} verified entries. Two Fetches kept the declared rt-tests row, with no change to other entries.\n')
PYTHON
  echo passed > "$r/verdict"; exit
fi
case "$arm" in x86_64) cpu=amd64;; aarch64) cpu=arm64;; *) exit 2;; esac
make -s list-all-images ARCH="$arm" DOCKER_BUILD=false STARTUP_OPTIONS="--bazelrc=$RUNNER_TEMP/images.bazelrc" | tr ' ' '\n' | grep -x benchmarks_hackbench
make rebuild-benchmarks_hackbench ARCH="$arm" DOCKER_BUILD=false STARTUP_OPTIONS="--bazelrc=$RUNNER_TEMP/images.bazelrc"
tag=gvisor.dev/images/benchmarks/hackbench:latest
docker image inspect "$tag" --format '{{json .Config}}' > "$r/docker-config.json"
test "$(docker image inspect "$tag" --format '{{.Architecture}}')" = "$cpu"
bz build --config="$arm" "$pkg:hackbench_image"
bz cquery --config="$arm" --output=files "$pkg:hackbench_image" >"$r/manifest-files"
python3 - "$r" "$arm" "$cpu" "$GITHUB_SHA" <<'PY'
import json, pathlib, sys,subprocess
r=pathlib.Path(sys.argv[1]); arch,cpu,carrier=sys.argv[2:]
v=[json.loads(pathlib.Path(p).read_text()) for p in (r/'manifest-files').read_text().splitlines()]
manifest,= (x for x in v if 'layers' in x)
config,= (x for x in v if 'rootfs' in x)
assert config['architecture']==cpu and config['config']['Cmd']==['/usr/bin/hackbench'] and len(manifest['layers'])==2,(config,manifest)
assert any('/usr/bin' in item for item in config['config']['Env'] if item.startswith('PATH='))
base=json.loads(subprocess.check_output(['git','show',f'{carrier}:test/rbe/hackbench-shipped.json']))['benchmarks/hackbench_'+arch]
total=sum(x['size'] for x in manifest['layers'])
(r/'sizes.json').write_text(json.dumps({'architecture':cpu,'shipped_manifest':base['manifest'],'shipped_compressed_bytes':base['total'],'candidate_compressed_bytes':total,'candidate_layers':manifest['layers'],'delta':total-base['total']},indent=2)+'\n')
print((r/'sizes.json').read_text())
PY

bz build @rt_tests//:COPYING
license=$(bz cquery --output=files @rt_tests//:COPYING)
docker run --rm --pull=never --runtime=runc --platform=linux/"$cpu" "$tag" cat /usr/share/doc/hackbench/COPYING > "$r/COPYING"
cmp "$r/COPYING" "$license"
sha256sum "$r/COPYING" > "$r/copying.sha256"
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
