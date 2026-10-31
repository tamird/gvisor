set -euo pipefail
cpu=$1
r="$RUNNER_TEMP/iperf-jammy/$cpu/transfer"; mkdir -p "$r"; exec > >(tee "$r/coordinator.log") 2>&1
uname -a; case "$cpu:$(uname -m)" in amd64:x86_64|arm64:aarch64);;*) exit 1;;esac
image=gvisor.dev/images/benchmarks/iperf
a="$RUNNER_TEMP/iperf-jammy/$cpu/oci"
test "$(cat "$a/archive.sha256")" = "$(sha256sum "$a/image.tar" | awk '{print $1}')"
grep -Fx 05f0c6feac4693941c8ccf6b8730ddf3c258a51c "$a/product"
docker load -i "$a/image.tar"
docker tag "$image:latest" "$image:$cpu"
test "$(docker image inspect "$image:$cpu" --format '{{.Architecture}}')" = "$cpu"
docker network create iperf-net
cleanup() { docker logs iperf-server >"$r/server.log" 2>&1 || :; docker logs iperf-client >"$r/client.json" 2>"$r/loader.log" || :; docker rm -f iperf-client iperf-server >/dev/null 2>&1 || :; docker network rm iperf-net >/dev/null 2>&1 || :; }; trap cleanup EXIT
docker run -d --name iperf-server --network iperf-net --user 65532:65532 "$image:$cpu" iperf3 -s --forceflush
for i in {1..20}; do docker logs iperf-server 2>&1 | grep -q 'Server listening' && break; sleep 1; done
docker logs iperf-server 2>&1 | grep 'Server listening'
docker run -d --name iperf-client --network iperf-net --user 65532:65532 -e LD_DEBUG=libs "$image:$cpu" iperf3 -c iperf-server -t 1 --connect-timeout 5000 --json
timeout 30s docker wait iperf-client >"$r/client-exit"
grep -Fx 0 "$r/client-exit"
docker logs iperf-client >"$r/client.json" 2>"$r/loader.log"
python3 - "$r" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]);j=json.loads((p/'client.json').read_text())
tx,rx=[j['end'][k]['bytes'] for k in ('sum_sent','sum_received')]
assert 'error' not in j and min(tx,rx)>0
(p/'verdict').write_text(f'PASS sent={tx} received={rx}\n')
PY
cat "$r/verdict"
docker run --rm --network iperf-net --user 65532:65532 "$image:$cpu" iperf3 -c iperf-server -t 1 -R -P 4 --json >"$r/reverse.json"
python3 - "$r/reverse.json" <<'PY'
import sys,json
j=json.load(open(sys.argv[1])); assert 'error' not in j and j['start']['test_start']['reverse'] and len(j['end']['streams'])==4
assert min(j['end'][k]['bytes'] for k in ('sum_sent','sum_received'))>0
PY
date -u '+NATIVE FORWARD AND FOUR-WAY REVERSE PASSED %FT%TZ' >"$r/both-verdict"
