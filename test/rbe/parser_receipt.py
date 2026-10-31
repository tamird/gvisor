"""Recover the real .o and classify all failures in the bounded compilation."""
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from parser_receipt_common import call, get, label, logs, pages, product

iid = "da0ec3e0-44ec-47bf-8ee7-79e086bb94bf"
old = "41d0faaf-0ed2-474b-93d7-6f9745ae03f4"
digest = "3aed290e7e6fba7d4bc3be56479288d8ac3ac91a1a18f36fafdbb37355e0b4b9"
dest = Path(os.environ["RUNNER_TEMP"]) / "parser-receipt"
dest.mkdir(exist_ok=True)
report = {"source": product, "invocation": iid, "original": old, "digest": digest}

def save():
    (dest / "original-object.json").write_text(json.dumps(report, indent=2) + "\n")

save()
for inv in (old, iid):
    rr = call("GetExecution", executionLookup={"invocationId": inv, "targetLabel": label},
              inlineExecuteResponse=True).get("execution", [])
    records = report[inv] = []
    for r in rr:
        if r.get("actionDigest", {}).get("hash") != digest:
            continue
        resp = r.get("executeResponse", {})
        a = resp.get("result", {})
        row = {"status": r.get("status", {}).get("code", 0), "exit": r.get("exitCode", 0),
               "linkType": r.get("invocationLinkType", "UNSPECIFIED"), "stage": r.get("stage"),
               "responseStatus": resp.get("status", {}).get("code", 0), "hasResult": bool(a),
               "responseExit": a.get("exitCode", 0), "outputs": []}
        records.append(row)
        for f in a.get("outputFiles", []):
            if not re.fullmatch(r"bazel-out/[^/]+/bin/tools/nvidia_driver_differ/_objs/driver_ast_parser/driver_ast_parser(?:\.pic)?\.o", f.get("path", "")):
                continue
            h, n = f["digest"]["hash"], int(f["digest"]["sizeBytes"])
            uri = f"bytestream://remote.buildbuddy.io/blobs/{h}/{n}"
            req = urllib.request.Request("https://app.buildbuddy.io/api/v1/GetFile",
                data=json.dumps({"uri": uri}).encode(),headers={"Content-Type":"application/json",
                "x-buildbuddy-api-key":os.environ["BUILDBUDDY_API_KEY"]})
            try:
                with urllib.request.urlopen(req, timeout=45) as response:
                    raw = response.read(n + 1)
            except urllib.error.HTTPError as exc:
                row["unavailableObject"] = {"sha256": h, "http": exc.code}
                continue
            assert len(raw) == n and hashlib.sha256(raw).hexdigest() == h
            assert raw[:6] == b"\x7fELF\x02\x01" and raw[16:20] == b"\x01\x00\x3e\x00"
            row["outputs"].append({"path": f["path"], "length": n, "sha256": h,
                                   "ELF": "AMD64 relocatable", "actualBytesVerified": True})
        save()

rr = call("GetExecution", executionLookup={"invocationId": iid}).get("execution", [])
bad = [r for r in rr if r.get("status", {}).get("code") or r.get("exitCode")]
report["remoteExecutionCount"] = len(rr)
report["unsuccessfulRemoteMnemonics"] = Counter(r.get("actionMnemonic", "?") for r in bad)
report["unsuccessful"] = [{"mnemonic": r.get("actionMnemonic"), "output": r.get("primaryOutputPath"),
    "label": r.get("targetLabel"), "linkType": r.get("invocationLinkType", "UNSPECIFIED"),
    "rpcCode": r.get("status", {}).get("code", 0), "exit": r.get("exitCode", 0)} for r in bad[:40]]
save()
errors = report["originalBazelFailure"] = []
for p in pages("GetLog", selector={"invocation_id": iid}):
    for line in p.get("log", {}).get("contents", "").splitlines():
        if not ("ERROR:" in line and ("failed:" in line or "error executing" in line)):
            continue
        # Capture only Bazel's action description and numeric exit, never the argv.
        m = re.search(r"BUILD(?:\.bazel)?:\d+:\d+: ([\w.,:/\-+@()\[\] =]{1,650}?) failed: \(Exit (\d+)\)", line)
        if m and len(errors) < 8:
            errors.append([m[1], int(m[2])])
report["verifiedObject"] = any(r["outputs"] for r in report[iid])
save()
print("original Bazel failing actions", errors, "actual .o", report["verifiedObject"])
