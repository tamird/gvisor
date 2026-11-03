"""Exercise the real TCP Nogo owner with and without the purge precondition."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(os.environ["RUNNER_TEMP"])
PRODUCT = "7f169af00dc2393add6e49671970a1459801c42b"
BASE = "471d6dff5229b99208bf6fec985601067f1f9d69"
PKG = Path("pkg/tcpip/transport/tcp")
ENDPOINT = PKG / "endpoint.go"
OWNER = "//pkg/tcpip/transport/tcp:tcp_nogo"
ARCHES = ("amd64", "arm64")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def collect(arm, result):
    events = ROOT / (arm + ".bep")
    for line in events.read_text().splitlines():
        e = json.loads(line)
        if "testResult" in e:
            attempt = e["testResult"]
            execution = attempt.get("executionInfo", {})
            result["attempts"].append(dict(owner=e["id"]["testResult"]["label"], status=attempt["status"],
                strategy=execution.get("strategy"), cached=attempt.get("cachedLocally", False) or execution.get("cachedRemotely", False)))
    log = (Path("bazel-testlogs") / PKG / "tcp_nogo/test.log").read_text()
    result["sections"] = re.findall(r"^(amd64|arm64)\.\.\.(PASS)?", log, re.M)
    valid = True
    for arch in ARCHES:
        data = (Path("bazel-bin") / PKG / ("tcp_nogo." + arch + ".findings")).read_bytes()
        rows = json.loads(data)
        if rows is None:
            rows = []
        matches = []
        for f in rows:
            pos = f["Position"]
            match = (f["Category"] == "checklocks" and Path(pos["Filename"]).name == "endpoint.go"
                     and pos["Line"] in result["access_lines"]
                     and "invalid field access" in f["Message"] and "ep.mu" in f["Message"]
                     and "must be locked when accessing pendingRcvdSegments" in f["Message"]
                     and f["GOARCH"] == arch and f["GOOS"] == "linux")
            if match:
                matches.append(dict(line=pos["Line"], column=pos["Column"], message=f["Message"]))
        result["findings"][arch] = dict(count=len(rows), matched=matches, sha256=hashlib.sha256(data).hexdigest())
        want = set(result["access_lines"]) if arm == "unguarded" else set()
        valid &= len(rows) == len(matches) and {m["line"] for m in matches} == want
    expected = "FAILED" if arm == "unguarded" else "PASSED"
    return (valid and sorted(result["sections"]) == [(a, "" if arm == "unguarded" else "PASS") for a in ARCHES]
            and len(result["attempts"]) == 1 and result["attempts"][0]["owner"] == OWNER
            and result["attempts"][0]["status"] == expected and not result["attempts"][0]["cached"]
            and result["attempts"][0]["strategy"] in {"processwrapper-sandbox", "linux-sandbox"}
            and result["exit"] == (3 if arm == "unguarded" else 0))


def run(arm, result):
    ref = BASE if arm == "unguarded" else PRODUCT
    subprocess.run(["git", "restore", "--source=" + ref, "--", ENDPOINT], check=True)
    if (git("rev-parse","HEAD") != PRODUCT or git("hash-object", str(ENDPOINT)) != git("rev-parse", ref + ":" + str(ENDPOINT))
            or git("diff","--name-only") != (str(ENDPOINT) if arm == "unguarded" else "")):
        raise ValueError("source identity")
    result["endpoint_blob"] = git("hash-object", str(ENDPOINT))
    result["receiver_blob"] = git("hash-object", str(PKG / "rcv.go"))
    result["access_lines"] = [i for i,s in enumerate(ENDPOINT.read_text().splitlines(),1)
                              if "e.rcv.pendingRcvdSegments" in s]
    if len(result["access_lines"]) != 2:
        raise ValueError("purge source lines")
    result["exit"] = subprocess.run(["bazel","test","--config=x86_64","-c","opt","--jobs=4",
        "--build_tag_filters=","--test_tag_filters=","--strategy=TestRunner=sandboxed","--nocache_test_results",
        "--flaky_test_attempts=1","--test_output=errors","--build_event_json_file=" + str(ROOT / (arm + ".bep")),OWNER]).returncode
    result["finished"] = datetime.now(timezone.utc).isoformat()
    result["verified"] = collect(arm, result)


def main():
    base_file = git("show", BASE + ":" + str(ENDPOINT))
    product_file = git("show", PRODUCT + ":" + str(ENDPOINT))
    marker = "func (e *Endpoint) purgePendingRcvQueue() {"
    if base_file.count(marker) != 1 or base_file.replace(marker, "// +checklocks:e.mu\n" + marker) != product_file:
        raise ValueError("control does not remove exactly the purge precondition")
    report = {"product":PRODUCT, "base":BASE, "owner":OWNER, "arms":{}}
    for arm in ("unguarded","guarded"):
        result = dict(attempts=[], sections=[], findings={}, verified=False)
        report["arms"][arm] = result
        try:
            run(arm, result)
        except Exception as exc:
            result["error"] = type(exc).__name__
    report["verdict"] = "PASS" if all(r["verified"] for r in report["arms"].values()) else "INCOMPLETE"
    report["verified_at"] = datetime.now(timezone.utc).isoformat()
    (ROOT / "tcp-heap-lock.json").write_text(json.dumps(report, indent=2) + "\n")
    print(report["verdict"], {a:r.get("exit") for a,r in report["arms"].items()})
    return report["verdict"] != "PASS"


if __name__ == "__main__":
    sys.exit(main())
