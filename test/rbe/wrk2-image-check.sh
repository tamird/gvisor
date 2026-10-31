set -euo pipefail
arm=$1
case "$arm" in x86_64) arch=amd64;; aarch64) arch=arm64;; *) exit 2;; esac
r="$RUNNER_TEMP/wrk2/$arm"
mkdir -p "$r"
exec > >(tee "$r/coordinator.log") 2>&1
git checkout -q --detach 106d6799384d037b2bd6359463603c5752a24a56
git rev-parse HEAD >"$r/source"
bz() { bazel --bazelrc="$RUNNER_TEMP/images.bazelrc" "$@"; }
export MAKEFLAGS=DOCKER_BUILD=false
make -s rebuild-benchmarks_wrk2 ARCH="$arm" DOCKER_BUILD=false STARTUP_OPTIONS="--bazelrc=$RUNNER_TEMP/images.bazelrc"
tag=gvisor.dev/images/benchmarks/wrk2:latest
docker image inspect "$tag" --format '{{.Architecture}} {{json .Config.Cmd}} {{.Size}}' >"$r/image"
grep -F "$arch [\"/usr/bin/wrk2\"]" "$r/image"
bz aquery --config="$arm" 'mnemonic("RunBinary", deps(@wrk2//:wrk2))' --output=jsonproto --include_commandline=true >"$RUNNER_TEMP/wrk2-aquery"
work=$(mktemp -d "$RUNNER_TEMP/wrk2-$arm.XXXX")
listener=
cleanup() { [[ -z "$listener" ]] || kill "$listener" 2>/dev/null || :; rm -rf "$work"; }
trap cleanup EXIT
docker image save -o "$work/image.tar" "$tag"
bz build --config="$arm" @wrk2//:LICENSE @wrk2//:NOTICE @luajit//:COPYRIGHT @openssl//:LICENSE.txt @zlib//:LICENSE
root=$(bz info execution_root)
for label in @wrk2//:LICENSE @wrk2//:NOTICE @luajit//:COPYRIGHT @openssl//:LICENSE.txt @zlib//:LICENSE; do
  f=$(bz cquery --config="$arm" --output=files "$label")
  name=${label#@}; name=${name//\/\/:/\/}
  printf '%s\t%s\n' "$name" "$root/$f"
done >"$work/paths"
python3 - "$work" "$r" "$RUNNER_TEMP/wrk2-aquery" <<'PY'
from pathlib import Path
import hashlib,io,json,sys,tarfile
w,r,aq=map(Path,sys.argv[1:])
a=json.loads(aq.read_text())
commands=[x['arguments'] for x in a['actions'] if x['mnemonic']=='RunBinary']
assert all(not v[0].endswith(('/bash','/sh')) for v in commands)
for exe in ('/minilua','/buildvm_'+('arm64' if r.name=='aarch64' else 'x64'),'/luajit_cli'):
    assert any(v[0].endswith(exe) for v in commands), (exe,[v[0] for v in commands])
(r/'generators.json').write_text(json.dumps(commands,indent=2)+'\n')
expected={'usr/share/doc/'+k:Path(v).read_bytes() for k,v in (line.split('\t') for line in (w/'paths').read_text().splitlines())}
with tarfile.open(w/'image.tar') as image:
    m,=json.load(image.extractfile('manifest.json')); assert len(m['Layers'])==2,m
    with tarfile.open(fileobj=io.BytesIO(image.extractfile(m['Layers'][-1]).read())) as app:
        data={k.name.removeprefix('./'):app.extractfile(k).read() for k in app if not k.isdir()}
assert set(data)=={'usr/bin/wrk2',*expected},data.keys()
assert all(data[k]==v for k,v in expected.items())
(w/'wrk2').write_bytes(data['usr/bin/wrk2'])
(r/'notices').write_text(''.join(f'{hashlib.sha256(v).hexdigest()}  {k}\n' for k,v in sorted(expected.items())))
PY
readelf -W -l -d "$work/wrk2" >"$r/elf"
readelf --dyn-syms -W "$work/wrk2" | grep -w luaJIT_BC_wrk >"$r/bytecode"
grep -F 'Requesting program interpreter' "$r/elf"
cat >"$work/serve.py" <<'PY'
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import sys
class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def do_GET(self):
        payload=b'wrk2 workload\n'
        self.send_response(200)
        self.send_header('Content-Length',str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def log_message(self,*args): pass
http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
Path(sys.argv[1]).write_text(str(http.server_port))
http.serve_forever()
PY
python3 "$work/serve.py" "$work/port" & listener=$!
for i in {1..20}; do [[ -s "$work/port" ]] && break; sleep 0.1; done
timeout 75s docker run --rm --network=host --platform="linux/$arch" --pull=never "$tag" wrk2 --threads 2 --connections 2 --rate 64 --duration 12 --timeout 5 --latency "http://127.0.0.1:$(cat "$work/port")/" >"$r/workload" 2>&1
grep -E ' [1-9][0-9]* requests in ' "$r/workload"
grep -E '^ *99\.000%' "$r/workload"
date -u '+PASS %FT%TZ' | tee "$r/verdict"
