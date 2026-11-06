"""Fetch the two ARM64 test ELF inputs recorded in the original Actions receipt."""

import hashlib
import json
import os
from pathlib import Path
import struct
import urllib.error
import urllib.request

t = Path(os.environ["RUNNER_TEMP"]) / "packet-pages"
key = os.environ.pop("BUILDBUDDY_API_KEY")
host = "remote.buildbuddy.io"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


old = json.loads((t / "original-receipt.json").read_text())
assert old["fixed"] == "dec1b82660c2974f5b98b2c5ab88d0857b34adb8"
assert old["stock"] == "1f49f256eae8d2dd2c69ea4cfe9ef2a5bb05516d"
report = {k: old[k] for k in ("stock", "fixed", "guest_source", "fixture_sha256")}
assert hashlib.sha256((t / "packet_ring_linux_test.go").read_bytes()).hexdigest() == report["fixture_sha256"]
report.update(commands={}, binaries={}, cas={}, **{"4k": {}})
for kind, sha, size in [("stock", "f81f5d8f1f7260f3876979411538b7962b27ec813b262ae5e6a60b8be8bea73f", 11822432),
                         ("fixed", "c08d9723ab6df7764e0b11d029bd645b0d05ecd928c89bddf9fea7973e60489e", 11822984)]:
    previous = old["binaries"][kind]
    assert previous["sha256"] == sha and previous["bytes"] == size and previous["source"] == old[kind]
    req = urllib.request.Request("https://" + host + "/api/v1/GetFile",
                                 data=json.dumps({"uri": f"bytestream://{host}/blobs/{sha}/{size}"}).encode(),
                                 headers={"x-buildbuddy-api-key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=120) as resp:
            data = resp.read(size + 1)
    except urllib.error.HTTPError as error:
        report["cas"][kind] = {"http": error.code}
        continue
    assert len(data) == size and hashlib.sha256(data).hexdigest() == sha
    assert data[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", data, 18)[0] == 183
    dest = t / ("arm64-" + kind)
    dest.write_bytes(data)
    dest.chmod(0o755)
    report["binaries"][kind] = previous
    report["cas"][kind] = {"verified": True}
(t / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")
