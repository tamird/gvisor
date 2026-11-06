"""Recover exactly one already-built release using the original CAS identity."""
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import urllib.error
import urllib.request

WORK = Path(os.environ["RUNNER_TEMP"]) / "chainguard-guest"
INV = "078dbb74-7b6d-44e9-a00f-276d3b82d5de"
OBJECTS = {
    "amd64": ("x86_64", "a9363f7ec1947a68d41dec93e8c9869be5577c9108f6a0c111e434cb5e9bac1a", 84393339),
    "arm64": ("aarch64", "ef28f19c89e43c617012fe1118388a84c49a8762bdf5978015f2d2334b7a0ee7", 77397545),
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


arch, = sys.argv[1:]
cpu, sha, size = OBJECTS[arch]
assert platform.machine() == cpu
receipt = {"arch": arch, "invocation": INV, "expectedSHA256": sha, "expectedBytes": size,
           "receivedBytes": 0, "verified": False}
try:
    url = "https://remote.buildbuddy.io/file/download?invocation_id=" + INV + "&bytestream_url=bytestream%3A%2F%2Fremote.buildbuddy.io%2Fblobs%2F" + sha + "%2F" + str(size)
    req = urllib.request.Request(url, headers={"x-buildbuddy-api-key":os.environ.pop("BUILDBUDDY_API_KEY")})
    with urllib.request.build_opener(NoRedirect).open(req,timeout=120) as response:
        receipt["http"] = response.status
        assert response.status == 200
        h = hashlib.sha256()
        with (WORK/"original.tar.bz2").open("xb") as dest:
            while receipt["receivedBytes"] < size:
                chunk = response.read(min(1<<20, size-receipt["receivedBytes"]))
                if not chunk:raise EOFError
                dest.write(chunk);h.update(chunk);receipt["receivedBytes"]+=len(chunk)
            assert not response.read(1)
        receipt["observedSHA256"] = h.hexdigest()
        receipt["verified"] = receipt["observedSHA256"]==sha
except urllib.error.HTTPError as e:
    receipt["http"] = e.code
except (OSError, EOFError, AssertionError) as e:
    receipt["errorType"] = type(e).__name__
finally:
    (WORK/"fetch.json").write_text(json.dumps(receipt,sort_keys=True)+"\n")
raise SystemExit(0 if receipt["verified"] else 1)
