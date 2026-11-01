"""Return only named TCP failures and their source diagnostics from one original."""
from hashlib import sha256
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
from xml.etree import ElementTree as ET

inv = "8351c413-99b1-450e-8f84-65516c391059"
prefix = "//test/rbe/forward_gso:"
key = os.environ["BUILDBUDDY_API_KEY"]
dest = Path(os.environ["RUNNER_TEMP"]) / "verdict.json"
out = {"invocation": inv, "owners": {}}


def rpc(method, req, raw=False):
    conn = HTTPSConnection("app.buildbuddy.io", timeout=25)
    try:
        conn.request("POST", "/api/v1/"+method, json.dumps(req), {
            "Content-Type":"application/json", "x-buildbuddy-api-key":key})
        with conn.getresponse() as resp:
            if resp.status != 200:
                raise RuntimeError(f"{method}: HTTP {resp.status}")
            data = resp.read(24*1024*1024+1)
            if len(data)>24*1024*1024:
                raise ValueError("oversized response")
            return data if raw else json.loads(data)
    finally:
        conn.close()


def pages(method, req):
    while True:
        d = rpc(method, req)
        yield d
        page = d.get("nextPageToken", d.get("next_page_token"))
        if not page:
            break
        req["page_token"] = page


def relevant(lines):
    return [line.strip()[:850] for line in lines if re.search(
        r"Traceback|(?:Error|Exception)(?:\(|:)|FATAL|error:|File \"|test timed out|not found|No space left|permission denied|timed out|Cannot|could not|failed to|timeout after|signal:", line, re.I)
        and not re.search("authorization|token|api.key|password|bearer|remote.header|bes.header", line, re.I)][-50:]



try:
    targets = {x["label"]:x.get("status") for page in pages("GetTarget",{"selector":{"invocation_id":inv}}) for x in page.get("target",[])}
    for module in ("forwarding_test",):
        label=prefix+module
        rec = out["owners"][label] = {"target":targets.get(label), "files":{}, "failures":{}}
        raw = {}
        for page in pages("GetAction",{"selector":{"invocation_id":inv,"target_label":label}}):
            for action in page.get("action",[]):
                for f in action.get("file", []):
                    name = f.get("name","").split("/")[-1]
                    if name not in ("test.log","test.xml"):continue
                    uri = f["uri"]
                    m = re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/(\d+)$", uri)
                    if not m or not uri.startswith("bytestream://"):raise ValueError("invalid artifact URI")
                    data=rpc("GetFile",{"uri":uri},raw=True)
                    if len(data)!=int(m[2]) or sha256(data).hexdigest()!=m[1]:raise ValueError("CAS checksum mismatch")
                    if raw.setdefault(name,data)!=data:raise ValueError("conflicting target output")
                    rec["files"][name]={"sha256":m[1],"bytes":len(data)}
        if "test.xml" in raw:
            cases=list(ET.fromstring(raw["test.xml"]).iter("testcase"))
            rec["cases"]=len(cases)
            for case in cases:
                for kind in ("error","failure"):
                    for err in case.findall(kind):
                        name=case.get("name","")
                        text=err.get("message","")+"\n"+"\n".join(err.itertext())
                        rec["failures"][name]=relevant(text.splitlines())
        if "test.log" in raw:
            # Child stderr precedes the Python harness's JSON report. Do
            # not export the raw report or command arguments.
            before = raw["test.log"].decode(errors="replace").split("\n{",1)[0].split("Traceback (most recent call last):",1)[0]
            safe = [line.strip()[:2000] for line in before.splitlines()
                    if line.strip() and not re.search(r"authorization|credential|token|api.key|password|bearer|(?:bes|remote).header|PosixPath\(|^exec ", line,re.I)]
            rec["startup"] = safe[:40]
    out["complete"] = all(x.get("failures") for x in out["owners"].values())
finally:
    dest.write_text(json.dumps(out,indent=2).replace(key,"[redacted]")+"\n")
