#!/bin/bash
set -euo pipefail
root="$PWD"
q="$root/test/rbe/python-launcher-compat"
up="$root/upstream"
reports="$root/results"
mkdir -p "$reports" client/tests/bootstrap_impls/bin_calls_bin
cd client
cp "$q/probe.py" .
printf '%s\n' 'written by upstream compatibility fixture' > payload.txt
cp "$q/client.MODULE" MODULE.bazel
cp "$q/client.WORKSPACE" WORKSPACE
cp "$q/smoke.BUILD" BUILD.bazel
cat "$q/extra.BUILD" >> BUILD.bazel
cp "$q/nested.BUILD" tests/bootstrap_impls/bin_calls_bin/BUILD.bazel
cp "$up"/tests/bootstrap_impls/bin_calls_bin/{outer,inner,inner_lib}.py tests/bootstrap_impls/bin_calls_bin/
sed 's/^set +e$/set -e/' "$up/tests/bootstrap_impls/run_binary_find_runfiles_test.sh" > run_binary_find_runfiles_test.sh
chmod +x run_binary_find_runfiles_test.sh
USE_BAZEL_VERSION=9.1.1 python3 "$q/check.py" stock "$reports/stock.json"
patch -d "$up" -p1 < "$q/rules_python.patch"
USE_BAZEL_VERSION=9.1.1 python3 "$q/check.py" native "$reports/native.json"
# Test the default WORKSPACE implementation with both supported Bazel lines.
cp "$q/smoke.BUILD" BUILD.bazel
for version in 7.4.1 8.5.1; do
  USE_BAZEL_VERSION="$version" python3 "$q/check.py" workspace "$reports/workspace-$version.json"
done
python3 - "$reports" <<'PY'
from pathlib import Path
import json,sys
d=Path(sys.argv[1]);r={p.stem:json.loads(p.read_text()) for p in d.glob('*.json')};print(json.dumps(r,indent=2))
assert all(v.get('build_exit')==0 for v in r.values()),r.keys()
assert all(v['checks']['ordinary']['exit']==0 for v in r.values())
s=r['stock']['checks'];n=r['native']['checks']
for k in ('symlink_one','symlink_two','upstream_symlink_test','sibling'):assert s[k]['exit']==0,(k,s[k])
assert s['sibling']['inner_import'] and n['empty_path']['exit']==0
assert all(n[k]['exit']!=0 for k in ('symlink_one','symlink_two','upstream_symlink_test')) and (n['sibling']['exit']!=0 or not n['sibling']['inner_import'])
PY
