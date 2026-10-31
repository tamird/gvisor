set -euo pipefail
arm=$1
product=d063c47347266f5f419dbbaf8dd75fa3c200fb96
r="$RUNNER_TEMP/hey-results/$arm"
mkdir -p "$r"
exec > >(tee "$r/coordinator.log") 2>&1
git checkout -q --detach "$product"
git rev-parse HEAD > "$r/product"
test "$(cat "$r/product")" = "$product"
bz() { bazel --bazelrc="$RUNNER_TEMP/images.bazelrc" "$@"; }
pkg=//images/benchmarks/hey
export MAKEFLAGS='DOCKER_BUILD=false'
if [[ "$arm" == catalog ]]; then
  bz build //tools/licensecheck/main:licensecheck
  lic=$(bz cquery --output=files //tools/licensecheck/main:licensecheck)
  "$lic" --mode=verify >"$r/verify.log" 2>&1 || { tail -12 "$r/verify.log"; exit 1; }
  doc=tools/licensecheck/dependencies.yaml
  perl -0777 -pe 's/^- dependency: github\.com\/rakyll\/hey\n(?:  [^\n]*\n)+//m' "$doc" > "$RUNNER_TEMP/hey-catalog.yaml"
  ! grep -Fqx -- '- dependency: github.com/rakyll/hey' "$RUNNER_TEMP/hey-catalog.yaml"
  "$lic" --mode=fetch --yaml="$RUNNER_TEMP/hey-catalog.yaml" >"$RUNNER_TEMP/hey-fetch1.log" 2>&1 || { tail -19 "$RUNNER_TEMP/hey-fetch1.log"; exit 1; }
  cp "$RUNNER_TEMP/hey-catalog.yaml" "$RUNNER_TEMP/hey-catalog.once"
  "$lic" --mode=fetch --yaml="$RUNNER_TEMP/hey-catalog.yaml" >"$RUNNER_TEMP/hey-fetch2.log" 2>&1 || { tail -19 "$RUNNER_TEMP/hey-fetch2.log"; exit 1; }
  cmp "$RUNNER_TEMP/hey-catalog.once" "$RUNNER_TEMP/hey-catalog.yaml"
  "$lic" --mode=verify --yaml="$RUNNER_TEMP/hey-catalog.yaml" >"$r/fetched-verify.log" 2>&1 || { tail -12 "$r/fetched-verify.log"; exit 1; }
  python3 - "$doc" "$RUNNER_TEMP/hey-catalog.yaml" "$r" <<'PY'
from pathlib import Path
import re, sys
source, fetched, out = map(Path, sys.argv[1:])
def rows(f):
    return {match[0]: match[1] for match in re.findall(r"(?m)^- dependency: ([^\n]+)\n((?:  [^\n]*\n)+)", f.read_text())}
old,new=rows(source),rows(fetched)
k="github.com/rakyll/hey"
assert set(old)==set(new),(set(old)-set(new),set(new)-set(old))
def stable(s):return re.sub(r"(?m)^  retrieved: [^\n]*\n","",s)
assert stable(new[k])==stable(old[k]),(old[k],new[k])
(out/"hey-catalog.txt").write_text("- dependency: "+k+"\n"+new[k])
changed=[x for x in old if x!=k and old[x]!=new[x]]
(out/"counts").write_text(f"Verified {len(old)} catalog entries. Two Fetch runs matched. {len(changed)} non-hey entries changed: {changed}\n")
print((out/"counts").read_text(),(out/"hey-catalog.txt").read_text())
PY
  echo passed > "$r/verdict"; exit
fi
case "$arm" in x86_64) cpu=amd64;; aarch64) cpu=arm64;; *) exit 2;; esac
make -s list-all-images ARCH="$arm" DOCKER_BUILD=false STARTUP_OPTIONS="--bazelrc=$RUNNER_TEMP/images.bazelrc" | tr ' ' '\n' | grep -x benchmarks_hey
make rebuild-benchmarks_hey ARCH="$arm" DOCKER_BUILD=false STARTUP_OPTIONS="--bazelrc=$RUNNER_TEMP/images.bazelrc"
tag=gvisor.dev/images/benchmarks/hey:latest
docker image inspect "$tag" --format '{{json .Config}}' > "$r/docker-config.json"
test "$(docker image inspect "$tag" --format '{{.Architecture}}')" = "$cpu"
bz build --config="$arm" "$pkg:hey_image"
bz cquery --config="$arm" --output=files "$pkg:hey_image" >"$r/manifest-files"
python3 - "$r" "$arm" "$cpu" "$GITHUB_SHA" <<'PY'
import json, pathlib, sys,subprocess
r=pathlib.Path(sys.argv[1]); arch,cpu,carrier=sys.argv[2:]
v=[json.loads(pathlib.Path(p).read_text()) for p in (r/'manifest-files').read_text().splitlines()]
manifest,= (x for x in v if 'layers' in x)
config,= (x for x in v if 'rootfs' in x)
assert config['architecture']==cpu and config['config']['Cmd']==['/bin/hey'] and len(manifest['layers'])==1,(config,manifest)
assert 'PATH=/bin' in config['config']['Env']
base=json.loads(subprocess.check_output(['git','show',f'{carrier}:test/rbe/hey-shipped.json']))['benchmarks/hey_'+arch]
total=sum(x['size'] for x in manifest['layers'])
(r/'sizes.json').write_text(json.dumps({'architecture':cpu,'shipped_manifest':base['manifest'],'shipped_compressed_bytes':base['total'],'candidate_compressed_bytes':total,'candidate_layers':manifest['layers'],'delta':total-base['total']},indent=2)+'\n')
print((r/'sizes.json').read_text())
PY
git checkout -q --detach "$GITHUB_SHA"
res=0; bz test --strategy=TestRunner=local --nocache_test_results --test_output=errors --test_arg=-test.v --test_timeout=120 //test/benchmarks/tools:hey_image_test || res=$?
d=bazel-testlogs/test/benchmarks/tools/hey_image_test
[[ ! -f "$d/test.log" ]] || cp "$d/test.log" "$r/test.log"
[[ ! -f "$d/test.xml" ]] || cp "$d/test.xml" "$r/test.xml"
[[ "$res" == 0 && -f "$r/test.xml" ]]
for name in TestHeyImage TestHeyImage/http TestHeyImage/https; do grep -F -- "--- PASS: $name " "$r/test.log"; done
! grep -E -- '--- (SKIP|FAIL): ' "$r/test.log"
echo passed > "$r/verdict"
