set -euo pipefail
arm=$1
product=4408065b6d4da2b749c16d4b1221e5d692f312e2
r="$RUNNER_TEMP/iperf-results/$arm"
mkdir -p "$r"
exec > >(tee "$r/coordinator.log") 2>&1
git checkout -q --detach "$product"
git rev-parse HEAD > "$r/product"
test "$(cat "$r/product")" = "$product"
bz() { bazel --bazelrc="$RUNNER_TEMP/images.bazelrc" "$@"; }
pkg=//images/benchmarks/hackbench
export MAKEFLAGS='DOCKER_BUILD=false'
if [[ "$arm" == catalog ]]; then
  git checkout --quiet --detach a8c4ea4999c5b6f9de7570286c1a5a3fa85ac0c0
  bz build //tools/licensecheck/main:licensecheck
  lic=$(bz cquery --output=files //tools/licensecheck/main:licensecheck)
  res=0; "$lic" --mode=verify >"$r/pre-registry.log" 2>&1 || res=$?
  echo "$res" >"$r/pre-registry-exit"
  grep -F -e BSD-3-Clause-LBNL -e HPND "$r/pre-registry.log" || true
  git checkout --quiet --detach "$product"
  bz build //tools/licensecheck/main:licensecheck
  lic=$(bz cquery --output=files //tools/licensecheck/main:licensecheck)
  doc=tools/licensecheck/dependencies.yaml
  cp "$doc" "$RUNNER_TEMP/hackbench-catalog.yaml"
  "$lic" --mode=verify >"$r/verify.log" 2>&1 || { tail -12 "$r/verify.log"; exit 1; }
  for run in 1 2; do
    "$lic" --mode=fetch --yaml="$RUNNER_TEMP/hackbench-catalog.yaml" >"$RUNNER_TEMP/hackbench-fetch$run.log" 2>&1 || { tail -20 "$RUNNER_TEMP/hackbench-fetch$run.log"; exit 1; }
    grep -F 'iperf' "$RUNNER_TEMP/hackbench-fetch$run.log" >"$r/fetch$run.log" || true
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
assert all(x in cur['iperf'] for x in ['BSD-3-Clause','BSD-3-Clause-LBNL','HPND','MIT']) and 'Apache-2.0' in cur['openssl']
(out/'license.txt').write_text(cur['iperf'])
(out/'counts').write_text(f'{len(cur)} verified entries. Two Fetches kept the declared iperf and OpenSSL rows, with no change to other entries.\n')
PYTHON
  echo passed > "$r/verdict"; exit
fi
