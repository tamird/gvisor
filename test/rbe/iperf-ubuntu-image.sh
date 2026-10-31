set -euo pipefail
arch=$1
case "$arch" in x86_64) cpu=amd64;; aarch64) cpu=arm64;; *) exit 2;; esac
r="$RUNNER_TEMP/iperf-jammy/$cpu/build"; mkdir -p "$r"
exec > >(tee "$r/coordinator.log") 2>&1
git checkout -q --detach 05f0c6feac4693941c8ccf6b8730ddf3c258a51c
git rev-parse HEAD >"$r/source"
bz() { bazel --bazelrc="$RUNNER_TEMP/images.bazelrc" "$@"; }
export MAKEFLAGS=DOCKER_BUILD=false
make -s rebuild-benchmarks_iperf ARCH="$arch" DOCKER_BUILD=false STARTUP_OPTIONS="--bazelrc=$RUNNER_TEMP/images.bazelrc"
pkg=//images/benchmarks/iperf:iperf_context
out=$(bz cquery --config="$arch" --output=files "$pkg"); test -f "$out"
il=$(bz cquery --config="$arch" --output=files @iperf//:LICENSE)
ol=$(bz cquery --config="$arch" --output=files @openssl//:LICENSE.txt)
root=$(bz info execution_root)
bz build --config="$arch" @iperf//:LICENSE @openssl//:LICENSE.txt
target="$RUNNER_TEMP/iperf-jammy/$cpu/oci"; mkdir -p "$target"
member=$(tar -tf "$out" | grep -Ex '(\./)?images/benchmarks/iperf/image.tar')
tar -xOf "$out" "$member" >"$target/image.tar"
sha256sum "$target/image.tar" | awk '{print $1}' >"$target/archive.sha256"
cp "$r/source" "$target/product"
tag=gvisor.dev/images/benchmarks/iperf
docker image inspect "$tag:latest" --format '{{json .Config}}' >"$r/docker-config.json"
docker image inspect "$tag:latest" --format '{{.Architecture}} {{.Size}}' >"$r/docker-image"
grep -F "$cpu " "$r/docker-image"
docker tag "$tag:latest" "$tag:$cpu"
python3 - "$target/image.tar" "$r" "$root/$il" "$root/$ol" "$cpu" <<'PY'
from pathlib import Path
import hashlib,io,json,sys,tarfile
image,r,il,ol=map(Path,sys.argv[1:5]);cpu=sys.argv[5]
cfg=json.loads((r/'docker-config.json').read_text());assert cfg['Cmd']==['/usr/bin/iperf3'],cfg
expected={'usr/share/doc/iperf3/LICENSE':il,'usr/share/doc/openssl/LICENSE.txt':ol}
with tarfile.open(image) as t:
    manifest,=json.load(t.extractfile('manifest.json'))
    config=json.load(t.extractfile(manifest['Config']));assert config['architecture']==cpu and config['os']=='linux'
    base,application=manifest['Layers']
    layers={}
    for name,label in [(base,'base'),(application,'application')]:
        layer=t.extractfile(name).read(); layers[label]={'sha256':hashlib.sha256(layer).hexdigest(),'bytes':len(layer)}
        with tarfile.open(fileobj=io.BytesIO(layer),mode='r:*') as tar:
            if label=='base':
                mode=next(m.mode for m in tar if m.name.removeprefix('./').rstrip('/')=='tmp')
                assert mode==0o1777,mode
            else:
                files={m.name.removeprefix('./'):tar.extractfile(m).read() for m in tar if not m.isdir()}
    assert set(files)=={'usr/bin/iperf3',*expected},set(files)
    for n,p in expected.items():assert files[n]==p.read_bytes(),n
    binary=files['usr/bin/iperf3'];assert int.from_bytes(binary[18:20],'little')=={'amd64':62,'arm64':183}[cpu]
    (r.parent/'iperf3').write_bytes(binary)
    (r/'contents.json').write_text(json.dumps({'layers':layers,'executableBytes':len(binary),'files':{n:hashlib.sha256(b).hexdigest() for n,b in files.items()},'baseTmp':oct(mode),'command':cfg['Cmd']},indent=2)+'\n')
PY
readelf -h -l -d "$r/../iperf3" >"$r/elf"
grep 'Requesting program interpreter' "$r/elf"
grep 'Shared library: \[libc.so.6\]' "$r/elf"
cp "$target/archive.sha256" "$r/archive.sha256"
date -u '+PASS %FT%TZ' >"$r/verdict"
