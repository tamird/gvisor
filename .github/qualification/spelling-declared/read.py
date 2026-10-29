"""Read the one original Ubuntu build_test without rerunning any workload."""

from datetime import datetime, timezone
from hashlib import sha256
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from xml.etree import ElementTree

ID = "9f33ba01-94bd-48a8-a812-8280acb4c57d"
OWNER = sys.argv[1]
assert OWNER in ("//:codespell_test", "//tools:lint_test")
KEY = os.environ.pop("ORIGINAL_READ_KEY")
OUT = Path(os.environ["RUNNER_TEMP"]) / (OWNER.split(":")[1]+".json")
result = {"invocation": ID, "owner": OWNER, "source": "84fb13cc46f39b7eed90cb43139fb1b2928fdac8", "original_read_only": True}


def check(ok, msg):
    if not ok:
        raise ValueError(msg)


def read(method, body, limit=12 << 20):
    paths = {"GetTarget": "/api/v1/GetTarget", "GetAction": "/api/v1/GetAction", "GetFile": "/api/v1/GetFile", "GetExecution": "/rpc/BuildBuddyService/GetExecution"}
    conn = HTTPSConnection("app.buildbuddy.io", timeout=40)
    try:
        conn.request("POST", paths[method], json.dumps(body),
                     {"content-type": "application/json", "x-buildbuddy-api-key": KEY})
        with conn.getresponse() as response:
            check(response.status == 200, f"{method}: HTTP {response.status}")
            data = response.read(limit + 1)
            check(len(data) <= limit, f"{method}: response size")
            return data if method == "GetFile" else json.loads(data)
    finally:
        conn.close()


def target_rows(method, selector):
    page = ""
    for _ in range(8):
        reply = read(method, {"selector": {"invocationId": ID, **selector}, "pageToken": page})
        yield from reply.get({"GetAction": "action", "GetTarget": "target"}[method], [])
        page = reply.get("nextPageToken")
        if not page:
            return
    raise ValueError("pagination limit")


def reports(actions):
    found = {}
    for a in actions:
        check(a["id"]["invocationId"] == ID and a["targetLabel"] == OWNER, "other action")
        for f in a.get("file", []):
            name = Path(f["name"]).name
            if name in {"test.xml", "test.log"} and "uri" in f:
                p = urlsplit(f["uri"])
                identity = re.fullmatch(r".*/blobs/([0-9a-f]{64})/(\d+)", p.path)
                check(p.scheme == "bytestream" and p.netloc == "remote.buildbuddy.io" and identity, "other file service")
                expected, size = identity.group(1), int(identity.group(2))
                check(size <= 2 << 20, "oversize report")
                data = read("GetFile", {"uri": f["uri"]}, size)
                check(len(data) == size and sha256(data).hexdigest() == expected, "report digest")
                check(name not in found or found[name] == data, "different original reports")
                found[name] = data
    return found


def executions():
    out = []
    response = read("GetExecution", {"executionLookup": {"invocationId": ID, "targetLabel": OWNER}, "inlineExecuteResponse": True})
    for e in response.get("execution", []):
        if e.get("targetLabel") not in (None, "", OWNER):
            continue
        er = e.get("executeResponse", {})
        a = er.get("result", {})
        cmd = e.get("commandSnippet", "")
        files = {}
        for f in a.get("outputFiles", []):
            name = Path(f["path"]).name
            if name in {"test.xml", "test.log"}:
                files[name] = {"sha256": f["digest"]["hash"], "bytes": int(f["digest"].get("sizeBytes", 0))}
        out.append({"action": e.get("actionDigest", {}).get("hash"), "stage": e.get("stage"),
                    "worker": bool(e.get("executorHostname") or e.get("executedActionMetadata", {}).get("worker") or a.get("executionMetadata", {}).get("worker")),
                    "exit": int(a.get("exitCode", 0)) if a else None, "status_codes": [int(v.get("status", {}).get("code", 0)) for v in (e, er)],
                    "cached": er.get("cachedResult", False), "test_setup": "test-setup.sh" in cmd,
                    "xml_generator": "generate-xml" in cmd or "generate_xml" in cmd,
                    "output": files, "action_result_present": bool(a)})
    return out


def main():
    targets = list(target_rows("GetTarget", {"label": OWNER}))
    result["target_status"] = [t.get("status") for t in targets]
    check(len(targets) == 1 and targets[0]["label"] == OWNER and targets[0]["id"]["invocationId"] == ID, "target identity")
    found = reports(list(target_rows("GetAction", {"targetLabel": OWNER})))
    facts = {n: {"sha256": sha256(d).hexdigest(), "bytes": len(d)} for n, d in found.items()}
    result["reports"] = facts
    rows = executions()
    result["physical"] = rows
    check(result["target_status"] == ["PASSED"] and set(found) == {"test.xml", "test.log"}, "target not executed")
    xml = ElementTree.fromstring(found["test.xml"])
    check(all(next(xml.iter(tag), None) is None for tag in ("failure", "error", "skipped")), "failing XML")
    result["xml_named_cases"] = [t.get("name") for t in xml.iter("testcase")]
    verified = [r for r in rows if r["stage"] in ("COMPLETED", 4) and r["worker"] and not r["cached"] and r["status_codes"] == [0, 0] and r["exit"] == 0]
    check(any(r["test_setup"] and r["output"].get("test.log") == facts["test.log"] for r in verified), "physical test wrapper / log")
    check(any(r["output"].get("test.xml") == facts["test.xml"] for r in verified), "physical XML")
    result["verdict"] = "PASS"


try:
    main()
except Exception as e:
    result.update(verdict="INCOMPLETE", error=type(e).__name__, reason=str(e)[:120] if isinstance(e, ValueError) else "")
finally:
    result["verified_at"] = datetime.now(timezone.utc).isoformat()
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
sys.exit(result["verdict"] != "PASS")
