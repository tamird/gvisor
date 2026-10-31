import json
import os
import subprocess
from pathlib import Path

triplet = [8192, 524288, 268435456]
keys = ["net.ipv4.tcp_rmem", "net.ipv4.tcp_wmem"]
probe = """
import json, pathlib, socket
with socket.socket() as s:
    print(json.dumps(dict(
        ranges=[list(map(int, pathlib.Path('/proc/sys/net/ipv4/tcp_'+x).read_text().split()))
                for x in ('rmem', 'wmem')],
        defaults=[s.getsockopt(socket.SOL_SOCKET, opt) for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF)])))
"""
def run(network, opts):
    args = ["docker", "run", "--rm", "--network", network]
    for key, values in opts.items():
        args.extend(["--sysctl", key+"="+" ".join(map(str, values))])
    return subprocess.run([*args, "--entrypoint", "/usr/bin/python3", os.environ["GNU"], "-c", probe], text=True, capture_output=True)

def private(opts):
    p = run("none", opts)
    if p.returncode:
        raise RuntimeError(p.stderr)
    return json.loads(p.stdout)

baseline = private({})
both = private(dict.fromkeys(keys, triplet))
partial = private({keys[0]: triplet[:2]})
assert both == dict(ranges=[triplet, triplet], defaults=[triplet[1]]*2), both
expected = dict(ranges=[[*triplet[:2], baseline["ranges"][0][2]], baseline["ranges"][1]],
                defaults=[triplet[1], baseline["defaults"][1]])
assert partial == expected, (partial, expected)
target = Path("/proc/sys/net/ipv4/tcp_rmem")
before = target.read_bytes()
shared = run("host", {keys[0]: before.decode().split()})
assert shared.returncode and keys[0] in shared.stderr and "host" in shared.stderr.lower(), (shared.returncode, shared.stderr)
assert target.read_bytes() == before
print(json.dumps(dict(kernel=os.uname().release, baseline=baseline, both=both, partial=partial, host_rejection=shared.stderr.strip())))
