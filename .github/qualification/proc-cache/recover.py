"""Materialize the eight digest-bound //:release files from each original build."""
import hashlib
import json
import os
from pathlib import Path
import re
import traceback
import urllib.parse
import urllib.request
import zipfile

from cas_inputs import KEY, OPENER, TMP, blob, fetch, rpc

ARCHIVE = "63a875b637f87427aac9a7bb2ce72638d6a1db71f678d2367e28a5c7eb037716"
IDS = {"base": "f58299e0-a527-48c1-b710-0ff68500cbed", "current": "8337f838-40aa-47ad-96b3-05f85df7946d"}
NAMES = {"runsc", "containerd-shim-runsc-v1", *("gvisor-bin/" + n for n in
         ("checkpointgofer", "gvisor-proc-umounter", "gvisor-sentry-prewarmer", "gvisor_sentry",
          "runsc-fd-parking", "runsc-metric-server"))}
OUT = TMP / "proc-cache-results"
OUT.mkdir(exist_ok=True)
r = {"status": "INCOMPLETE", "original": {"run": 41417639436, "artifact": 13128918637, "sha256": ARCHIVE,
     "timed_out_command": "run --detach", "timeout_seconds": 120, "measurement_count": 0},
     "carrier": os.environ["GITHUB_SHA"], "recovered": {}}


def persist():
    (OUT / "recovery.json").write_text(json.dumps(r, indent=2) + "\n")


def inventory(reply, digests, sizes):
    if isinstance(reply, list):
        for item in reply:
            inventory(item, digests, sizes)
        return
    if not isinstance(reply, dict):
        return
    name_value = reply.get("name") or reply.get("path") or ""
    name = Path(name_value).name if isinstance(name_value, str) else ""
    digest = reply.get("digest", reply.get("hash", ""))
    size = reply.get("length", reply.get("sizeBytes", 0))
    if isinstance(digest, dict):
        size, digest = digest.get("sizeBytes"), digest.get("hash")
    uri = re.fullmatch(r"bytestream://[^/]+/.*?blobs/([0-9a-f]{64})/(\d+)", str(reply.get("uri", "")))
    if uri:
        digest, size = uri.groups()
    if name in digests and digest == digests[name] and size:
        sizes[name].add(int(size))
    for v in reply.values():
        inventory(v, digests, sizes)


def recover(arm, uid, build):
    assert build["source"] == os.environ[{"base":"BASE","current":"SOURCE"}[arm]]
    assert build["result"] == "PASS" and build["exit"] == 0 and build["completed"] == ["//:release"]
    assert build["invocations"] == ["https://app.buildbuddy.io/invocation/" + uid]
    outputs = build["outputs"]
    assert set(outputs) == NAMES
    digests = {Path(name).name: h for name, h in outputs.items()}
    sizes = {name: set() for name in digests}
    inventory(rpc("GetTarget", {"invocationId": uid, "targetLabel": "//:release"}), digests, sizes)
    if any(not s for s in sizes.values()):
        inventory(json.loads(fetch("api/v1/GetAction", {"selector": {"invocation_id": uid, "target_label": "//:release"}})), digests, sizes)
    r["recovered"][arm] = {"uuid": uid, "matches": {name:sorted(values) for name,values in sizes.items()}, "files": {}}
    persist()
    for label, h in outputs.items():
        size, = sizes[Path(label).name]
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
        assert set(z.namelist()) == {"base-build.json", "current-build.json", "comparison.json"}
        failed = json.loads(z.read("comparison.json"))
        assert failed["status"] == "INCOMPLETE" and len(failed["trials"]) == 1 and failed["trials"][0]["phase"] == "run"
        r["original"]["result"] = failed
        persist()
        for arm, uid in IDS.items():
            name = arm + "-build.json"
            (OUT / name).write_bytes(z.read(name))
            recover(arm, uid, json.loads(z.read(name)))
    r["status"] = "PASS"
except Exception as exc:
    r["failure"] = {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
    raise
finally:
    persist()
