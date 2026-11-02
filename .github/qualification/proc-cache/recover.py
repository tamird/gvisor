"""Materialize the eight digest-bound //:release files from the original baseline build."""
import hashlib
import json
import os
from pathlib import Path
import re
import traceback
import urllib.parse
import urllib.request
import zipfile

from cas_inputs import KEY, OPENER, TMP, blob

ARCHIVE = "9b3c848f003a950d5f97657d1aedd455cb128ad47e34d970194da6fd7e810967"
INVOCATION = "8337f838-40aa-47ad-96b3-05f85df7946d"
NAMES = {"runsc", "containerd-shim-runsc-v1", *("gvisor-bin/" + n for n in
         ("checkpointgofer", "gvisor-proc-umounter", "gvisor-sentry-prewarmer", "gvisor_sentry",
          "runsc-fd-parking", "runsc-metric-server"))}
OUT = TMP / "proc-cache-results"
OUT.mkdir(exist_ok=True)
r = {"status": "INCOMPLETE", "original": {"run": 41420373860, "artifact": 13129258435, "sha256": ARCHIVE},
     "carrier": os.environ["GITHUB_SHA"], "recovered": {}}


def persist():
    (OUT / "recovery.json").write_text(json.dumps(r, indent=2) + "\n")


def recover(build, files):
    arm, uid = "base", INVOCATION
    assert build["source"] == os.environ["BASE"]
    assert build["result"] == "PASS" and build["exit"] == 0 and build["completed"] == ["//:release"]
    assert build["invocations"] == ["https://app.buildbuddy.io/invocation/" + uid]
    outputs = build["outputs"]
    assert set(outputs) == set(files) == NAMES
    assert all(digest == files[label][0] for label, digest in outputs.items())
    r["recovered"][arm] = {"uuid": uid, "files": {}}
    persist()
    for label, h in outputs.items():
        size = files[label][1]
        assert 0 < size <= 256 << 20 and re.fullmatch(r"[0-9a-f]{64}", h)
        path = TMP / ("install-" + arm) / label
        path.parent.mkdir(parents=True, exist_ok=True)
        if size <= 64 << 20:
            path.write_bytes(blob({"hash": h, "sizeBytes": size}, uid))
        else:
            url = "https://app.buildbuddy.io/file/download?" + urllib.parse.urlencode({
                "bytestream_url": f"bytestream://remote.buildbuddy.io/blobs/{h}/{size}", "invocation_id": uid})
            req = urllib.request.Request(url, headers={"x-buildbuddy-api-key": KEY})
            with OPENER.open(req, timeout=75) as response, path.open("wb") as out:
                left = size
                while left:
                    chunk = response.read(min(left, 1 << 20))
                    assert chunk
                    out.write(chunk)
                    left -= len(chunk)
                assert response.read(1) == b""
        with path.open("rb") as f:
            assert path.stat().st_size == size and hashlib.file_digest(f, "sha256").hexdigest() == h
        path.chmod(0o555)
        r["recovered"][arm]["files"][label] = [h, size]
        persist()


try:
    p = TMP / "original.zip"
    assert hashlib.sha256(p.read_bytes()).hexdigest() == ARCHIVE
    with zipfile.ZipFile(p) as z:
        recovered = json.loads(z.read("recovery.json"))
        assert recovered["status"] == "PASS" and json.loads(z.read("comparison.json"))["status"] == "PASS"
        previous = recovered["recovered"]["current"]
        assert previous["uuid"] == INVOCATION
        raw = z.read("current-build.json")
        (OUT / "base-build.json").write_bytes(raw)
        recover(json.loads(raw), previous["files"])
    r["status"] = "PASS"
except Exception as exc:
    r["failure"] = {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
    raise
finally:
    persist()
