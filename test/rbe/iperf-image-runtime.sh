set -euo pipefail
arm=$1
case "$arm" in x86_64) cpu=amd64;; aarch64) cpu=arm64;; *) exit 2;; esac
r="$RUNNER_TEMP/iperf-results/image-$arm"; mkdir -p "$r"; exec > >(tee "$r/coordinator.log") 2>&1
prod=a8c4ea4999c5b6f9de7570286c1a5a3fa85ac0c0
git checkout -q --detach "$prod"; git rev-parse HEAD >"$r/product"; grep -Fx "$prod" "$r/product"
bz() { bazel --bazelrc="$RUNNER_TEMP/images.bazelrc" "$@"; }
export MAKEFLAGS=DOCKER_BUILD=false
make -s list-all-images ARCH="$arm" DOCKER_BUILD=false STARTUP_OPTIONS="--bazelrc=$RUNNER_TEMP/images.bazelrc" | tr ' ' '\n' | grep -x benchmarks_iperf
make -s rebuild-benchmarks_iperf ARCH="$arm" DOCKER_BUILD=false STARTUP_OPTIONS="--bazelrc=$RUNNER_TEMP/images.bazelrc"
tag=gvisor.dev/images/benchmarks/iperf:latest
test "$(docker image inspect "$tag" --format '{{.Architecture}}')" = "$cpu"
docker image inspect "$tag" --format '{{json .Config}}' >"$r/config.json"
work=$(mktemp -d "$RUNNER_TEMP/iperf-image-XXXX")
tagged=iperf-$cpu-$GITHUB_RUN_ID
net=iperf-net-$cpu-$GITHUB_RUN_ID
cleanup() { docker rm -f "$tagged" >/dev/null 2>&1 || true; docker network rm "$net" >/dev/null 2>&1 || true; rm -rf "$work"; }
trap cleanup EXIT
docker image save -o "$work/oci.tar" "$tag"
bz build --config="$arm" @iperf//:LICENSE @openssl//:LICENSE.txt
execroot=$(bz info execution_root)
il=$(bz cquery --config="$arm" --output=files @iperf//:LICENSE)
ol=$(bz cquery --config="$arm" --output=files @openssl//:LICENSE.txt)
python3 - "$work" "$r" "$execroot/$il" "$execroot/$ol" <<'PY'
import io,hashlib,json,sys,tarfile
from pathlib import Path
w,r,il,ol=map(Path,sys.argv[1:]); expected={'usr/share/doc/iperf3/LICENSE':il,'usr/share/doc/openssl/LICENSE.txt':ol}
cfg=json.loads((r/'config.json').read_text());assert cfg['Cmd']==['/usr/bin/iperf3'] and cfg['User']=='0:0' and 'PATH=/usr/bin:/bin' in cfg['Env'],cfg
files={}; dirs={}; members=[]
with tarfile.open(w/'oci.tar') as image:
    manifest,=json.load(image.extractfile('manifest.json')); assert len(manifest['Layers'])==2,manifest
    for name in manifest['Layers']:
        with tarfile.open(fileobj=io.BytesIO(image.extractfile(name).read()),mode='r:*') as layer:
            for m in layer.getmembers():
                path=m.name.removeprefix('./').rstrip('/')
                members.append((path,oct(m.mode),m.size)); assert not m.issym() and not m.islnk(),m.name
                if m.isdir(): dirs[path]=m.mode
                elif m.isfile(): files[path]=layer.extractfile(m).read()
assert set(files)=={'usr/bin/iperf3',*expected},set(files)
assert dirs['tmp']==0o1777,dirs
for path,original in expected.items(): assert files[path]==original.read_bytes(),path
(w/'iperf3').write_bytes(files['usr/bin/iperf3'])
(r/'members.json').write_text(json.dumps(members,indent=2)+'\n')
(r/'notice-checksums').write_text(''.join(f'{hashlib.sha256(files[k]).hexdigest()}  {k}\n' for k in sorted(expected)))
PY
readelf -h -l -d "$work/iperf3" >"$r/elf"
if grep -Eq 'INTERP|\(NEEDED\)' "$r/elf"; then echo 'static executable acquired a runtime loader or dependency' >&2; exit 1; fi
docker network create "$net" >/dev/null
docker run --user 65532:65532 --name "$tagged" --detach --pull=never --runtime=runc --platform="linux/$cpu" --network="$net" "$tag" iperf3 -s --forceflush >/dev/null
for _ in $(seq 60); do if docker logs "$tagged" 2>&1 | grep -q 'Server listening'; then break; fi; sleep 1; done
docker logs "$tagged" 2>&1 | grep 'Server listening'
docker run --rm --user 65532:65532 --pull=never --runtime=runc --platform="linux/$cpu" --network="$net" "$tag" iperf3 -c "$tagged" -t 1 --json >"$r/client.json"
python3 - "$r/client.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]));assert not d.get('error') and min(d['end'][k]['bytes'] for k in ('sum_sent','sum_received'))>0,d
print('unprivileged transfer bytes',d['end']['sum_sent']['bytes'],d['end']['sum_received']['bytes'])
PY
docker tag "$tag" "gvisor.dev/images/benchmarks/iperf:$cpu"
echo PASS >"$r/verdict"
