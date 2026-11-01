"""Verify the one missing TCP analyzer in its original invocation or its recovery."""
from hashlib import sha256
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
import sys
from xml.etree import ElementTree

root = Path(os.environ["RUNNER_TEMP"]) / "tcp-evidence"
product = os.environ["PRODUCT"]
inv = "19e2bc95-c8a2-44f6-9efe-833ac8bc7ea1"
owner = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test_nogo"


def write(name, row):
    (root / name).write_text(json.dumps(row, indent=2) + "\n")


def rpc(method, payload, raw=False):
    connection = HTTPSConnection("app.buildbuddy.io", timeout=24)
    try:
        connection.request("POST", "/api/v1/" + method, json.dumps(payload), {
            "Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        with connection.getresponse() as r:
            if r.status != 200:
                raise RuntimeError(f"{method} HTTP {r.status}")
            body = r.read(16*1024*1024+1)
            if len(body) > 16*1024*1024:
                raise ValueError("Oversized response")
            return body if raw else json.loads(body)
    finally:
        connection.close()


def pages(method, request):
    while True:
        result = rpc(method, request)
        yield result
        page = result.get("nextPageToken", result.get("next_page_token"))
        if not page:
            break
        request["page_token"] = page


def inspect(raw):
    out = {}
    for name, data in raw.items():
        out[name] = {"sha256": sha256(data).hexdigest(), "bytes": len(data)}
        if name == "test.log":
            out["architectures"] = [arch for arch in ("amd64","arm64") if arch.encode()+b"..." in data]
        else:
            cases = list(ElementTree.fromstring(data).iter("testcase"))
            out.update(xml_cases=len(cases), failures=sum(
                any(c.find(kind) is not None for kind in ("failure","error","skipped")) for c in cases))
    out["verified"] = (set(raw) == {"test.log","test.xml"} and
                       out["architectures"] == ["amd64","arm64"] and out["xml_cases"] > 0 and not out["failures"])
    return out


def phase(text):
    # Only report known Bazel phase names, owners and numeric durations.
    m = re.findall(r"(?:Analyzing|Testing|Compiling) (?:@@)?//(?:pkg/|test/)[a-zA-Z0-9_/+.:-]+(?:; [\d,]+s)?", text)
    return m[-10:]


def prior():
    req = {"selector": {"invocation_id": inv}}
    targets = {t["label"]:t.get("status") for page in pages("GetTarget",req) for t in page.get("target",[])}
    row = {"source":product,"invocation":inv,"label":owner,"status":targets.get(owner),
           "owning_tcp":targets.get("//pkg/tcpip/transport/tcp:tcp_nogo")}
    write("original-analyzer.json",row)
    if not row["owning_tcp"]:
        raise ValueError("Could not identify original two-owner invocation")
    files = {}
    for result in pages("GetAction", {"selector":{"invocation_id":inv,"target_label":owner}}):
        for a in result.get("action", []):
            for f in a.get("file", []):
                name = f.get("name","").split("/")[-1]
                if name not in ("test.log","test.xml"):
                    continue
                u = f["uri"]
                m = re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/(\d+)$",u)
                if not m or not u.startswith("bytestream://"):
                    raise ValueError("Unsupported original CAS identifier")
                data = rpc("GetFile",{"uri":u},raw=True)
                if len(data) != int(m[2]) or sha256(data).hexdigest() != m[1]:
                    raise ValueError("Original checksum mismatch")
                if files.setdefault(name,data) != data:
                    raise ValueError("Ambiguous original result")
    row.update(inspect(files))
    # A successfully reported target is not proof of both architectures without the log.
    success = row["verified"] and row["status"] == "PASSED"
    row["verified"] = bool(success)
    if not success:
        progress = []
        for page in pages("GetLog", {"selector": {"invocation_id": inv}}):
            progress.extend(phase(page.get("log",{}).get("contents","")))
        row["last_actions"] = list(dict.fromkeys(progress[-35:]))
    write("original-analyzer.json",row)
    with open(os.environ["GITHUB_OUTPUT"],"a") as output:
        print("result=" + ("present" if success else "missing"), file=output)
    if files and (row.get("failures") or row["status"] in ("FAILED","TIMED_OUT","FLAKY")):
        raise ValueError("The original completed with a failed test")
    if success:
        write("verdict.json", row | {"qualification":"original"})


def current():
    ids, statuses, actions = [], {}, []
    if (root/"bep").is_file():
        for line in (root/"bep").open():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if "started" in event: ids.append(event["started"]["uuid"])
            if "testResult" in event: actions.append(event["id"]["testResult"]["label"])
            if "testSummary" in event: statuses[event["id"]["testSummary"]["label"]]=event["testSummary"]["overallStatus"]
    path = Path("bazel-testlogs")/owner.removeprefix("//").replace(":","/")
    row = inspect({name:(path/name).read_bytes() for name in ("test.log","test.xml") if (path/name).is_file()})
    rc = int((root/"exit").read_text())
    row.update(source=product,invocations=ids, label=owner,actions=actions,statuses=statuses,
               start=(root/"start").read_text().strip(),end=(root/"end").read_text().strip(),exit=rc,
               last_actions=phase((root/"log").read_text(errors="replace")),qualification="recovered")
    row["verified"] &= rc==0 and len(ids)==1 and statuses=={owner:"PASSED"} and actions==[owner]
    write("verdict.json", row)
    if not row["verified"]: raise SystemExit(1)


if sys.argv[1] == "original":
    prior()
elif sys.argv[1] == "current":
    current()
else:
    raise SystemExit("Unknown scope")
