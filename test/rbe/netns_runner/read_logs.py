"""Read only the previously measured log hashes and retain fixture diagnostics."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit
import zipfile

from cas_inputs import fetch


TARGET = "//runsc/sandbox:sandbox_test"
ROOT = Path(os.environ["RUNNER_TEMP"]) / "release-rbe"
EXPECTED = {
    "baseline.zip": "4937e522bb5189db00461daa084b2c4431baf1aabfd50309812f719f9eb0599a",
    "candidate.zip": "05d4ad33ef4bd89d453b0596c8dee81f8a762a00bd3fa219e277bb7dfc0a37e2",
}


def project(raw):
    result = {}
    case = None
    pair = {}
    for text in raw.decode(errors="replace").splitlines():
        name = re.search(r"(?:=== RUN\s+|--- FAIL: )(TestCollectLinksAndRoutes_[A-Za-z]+)", text)
        if name:
            case = result.setdefault(name[1], {"diagnostics": []})
        if case is None:
            continue
        msg = re.search(r"network_test\.go:(\d+):\s+([^:]+)", text)
        if msg:
            # Only the location, the source-written diagnostic prefix and the
            # errno vocabulary. Do not publish the original line or paths.
            category = re.match(r"(Failed to [A-Za-z0-9 /_-]+|[A-Za-z0-9]+ mismatch|collectLinksAndRoutes failed)", msg[2])
            reason = [r for r in ("operation not permitted","permission denied","file exists",
                                  "no such file or directory","invalid argument","not supported",
                                  "network is unreachable","read-only file system","file not found",
                                  "cannot assign requested address") if r in text.lower()]
            pair = {"line": int(msg[1]), "message": category[1].strip() if category else "assertion", "reasons": reason}
            case["diagnostics"].append(pair)
        data = re.match(r"^\s*(got|want)\s+(.+)$", text)
        if data and pair:
            value = data[2]
            # The test's topology is synthetic. Report the order and multiplicity
            # of fields used by its comparators; normalize any unrecognized value.
            known = set(re.findall(r"(?:10|192|2001|127)[.0-9a-fA-F:/]+", (
                Path("runsc/sandbox/network_test.go").read_text())))
            facts = {}
            for key, v in re.findall(r"\b(Name|MTU|QDisc|Address|PrefixLen|Destination|Gateway|IP|LinkAddress|HardwareAddr):([^\s{}\]]+)", value):
                if key in ("LinkAddress", "HardwareAddr"):
                    v = "length-" + str(len(v))
                elif not (v in known or v.rstrip("/") in ("::1","<nil>","0.0.0.0","::","ff00::/8","fe80::/64")
                          or re.fullmatch(r"(?:testveth[0-9]+(?:-peer)?|lo|none|fifo|[0-9]{1,6})", v)):
                    v = "link-local-v6" if v.startswith("fe80:") else "other"
                facts.setdefault(key, []).append(v)
            pair[data[1]] = facts
    return {k:v for k,v in result.items() if v["diagnostics"]}


def get_size(inv, digest):
    reply = json.loads(fetch("rpc/BuildBuddyService/GetTarget", {"invocationId": inv, "targetLabel": TARGET}))
    sizes = set()
    for g in reply.get("targetGroups", []):
        for t in g.get("targets", []):
            if t.get("metadata",{}).get("label") != TARGET:
                continue
            for e in t.get("testResultEvents", []):
                for f in e["testResult"]["testActionOutput"]:
                    if f["name"] != "test.log":
                        continue
                    uri = urlsplit(f.get("uri", ""))
                    parts = uri.path.split("/")
                    if len(parts) > 2 and parts[-2] == digest and parts[-1].isdigit():
                        sizes.add(int(parts[-1]))
                    elif f.get("digest") == digest:
                        sizes.add(int(f["length"]))
    size, = sizes
    return size


def read_all():
    sources = {}
    for f, digest in EXPECTED.items():
        data = (ROOT / f).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if f == "baseline.zip":
                for arch in ("amd64","arm64"):
                    r = json.loads(z.read(arch + ".json"))
                    log = r["targets"][TARGET]["results"][0]["failure_log"]
                    sources[arch+"-baseline"] = (r["events"]["invocation"], log["sha256"], log["bytes"])
            else:
                for n in z.namelist():
                    r = json.loads(z.read(n))
                    sources[n[:-5]] = (r["invocation"], r["log_sha256"], None)
    out = {}
    for key,(inv,digest,size) in sources.items():
        record = out[key] = {"invocation":inv,"sha256":digest}
        try:
            size = size or get_size(inv, digest)
            assert 0 < size < 1048576
            uri = "bytestream://remote.buildbuddy.io/blobs/" + digest + "/" + str(size)
            raw = fetch("file/download?" + urlencode({"invocation_id":inv,"bytestream_url":uri}))
            assert len(raw) == size and hashlib.sha256(raw).hexdigest() == digest
            record.update(bytes=size, cases=project(raw))
        except (KeyError, OSError, ValueError, AssertionError, TypeError) as e:
            record["error"] = type(e).__name__
            record["http"] = getattr(e, "code", None)
    (ROOT / "public" / "route-logs.json").write_text(json.dumps(out,indent=2)+"\n")
    return all(r.get("cases") for r in out.values())


if __name__ == "__main__":
    raise SystemExit(not read_all())
