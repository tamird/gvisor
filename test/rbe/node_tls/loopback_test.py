"""Exercise the reviewed TLS regression in the Node image used by presubmit."""

import hashlib
import os
from pathlib import Path
import subprocess
import unittest

IMAGE = (
    "us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/"
    "runtimes/nodejs22.2.0_x86_64@sha256:"
    "68af6d298e743e15b8e8b518393d4dd023dcb8209d718bd9d0fcae66a088006e"
)
ROOT = "/root/node-v22.2.0"
TEST = "test/parallel/test-tls-autoselectfamily-servername.js"
HERE = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"] / "test/rbe/node_tls"
OUT = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
NETWORK_CHECK = """
const assert = require('node:assert/strict');
const os = require('node:os');
const net = require('node:net');
assert.equal(process.version, 'v22.2.0');
assert.ok(process.versions.openssl);
const addresses = Object.values(os.networkInterfaces()).flat();
console.log(JSON.stringify({ version: process.version, openssl: process.versions.openssl, addresses }));
assert.ok(addresses.length && addresses.every((a) => a.internal));
const socket = net.connect(443, '192.0.2.1');
socket.setTimeout(3000, () => { throw new Error('external probe timed out'); });
socket.once('connect', () => { throw new Error('external route available'); });
socket.once('error', (e) => {
  console.log('external control:', e.code);
  assert.ok(['ENETUNREACH', 'EHOSTUNREACH'].includes(e.code), e);
});
"""


class LoopbackTest(unittest.TestCase):
    def test_native_and_systrap(self):
        source = (HERE / "reviewed.js").resolve()
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                         "2e05bada553040d251addaf86a9b6aa0d2dd5637ea5251dd778465eba41cd593")
        for runtime in ("runc", "runsc-systrap"):
            args = [
                "docker", "run", "--rm", "--pull=never", "--network=none",
                f"--runtime={runtime}", "--workdir", ROOT,
                "--mount", f"type=bind,src={source},dst={ROOT}/{TEST},readonly",
                "--entrypoint", f"{ROOT}/out/Release/node",
            ]
            for label, options in (("network", [IMAGE, "-e", NETWORK_CHECK]),
                                   ("tls", ["--env", "NODE_DEBUG=net,tls", IMAGE, TEST])):
                with self.subTest(runtime=runtime, label=label):
                    result = subprocess.run([*args, *options], text=True, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, timeout=40)
                    (OUT / f"{runtime}-{label}.log").write_text(result.stdout)
                    print(f"{runtime} {label} exit={result.returncode}", flush=True)
                    print(result.stdout, flush=True)
                    self.assertEqual(result.returncode, 0)
                    if label == "tls":
                        self.assertIn("127.0.0.1", result.stdout)
                        self.assertIn("::1", result.stdout)
                        self.assertIn("agent1", result.stdout)


if __name__ == "__main__":
    unittest.main()
