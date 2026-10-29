"""Resolve the sole already-finished native owner; verify its original output URIs."""
from datetime import datetime
from decimal import Decimal
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
import sys

from verify import OWNER, SOURCE, verify, verified_file

METHODS = {"/rpc/BuildBuddyService/SearchInvocation", "/api/v1/GetInvocation",
           "/api/v1/GetTarget", "/api/v1/GetAction"}
KEY = os.environ.pop("ORIGINAL_READ_KEY")


def get(method, request):
    assert method in METHODS
    conn = HTTPSConnection("app.buildbuddy.io", timeout=30)
    try:
        conn.request("POST", method, json.dumps(request),
                     {"Content-Type": "application/json", "x-buildbuddy-api-key": KEY})
        with conn.getresponse() as response:
            if response.status != 200:
                raise ValueError(f"{method}: HTTP {response.status}")
            data = response.read((8 << 20) + 1)
            assert len(data) <= 8 << 20
            return json.loads(data)
    finally:
        conn.close()


def query(**fields):
    q = dict(pattern=OWNER, command="test", updatedAfter="2026-10-29T10:25:00Z",
             updatedBefore="2026-10-29T10:32:30Z", **fields)
    response = get("/rpc/BuildBuddyService/SearchInvocation", {"query": q, "count": 25})
    assert not response.get("nextPageToken")
    return response.get("invocation", [])


def records(method, **selector):
    return get("/api/v1/Get" + method, {"selector": selector}).get(method.lower(), [])


def main(temp):
    out = temp / "phase-results"
    candidates = []
    try:
        candidates = query(commitSha=SOURCE)
        if not candidates:
            candidates = query()
    except ValueError:
        pass
    if not candidates:
        candidates = [dict(r, invocationId=r["id"]["invocationId"])
                      for r in records("Invocation", commit_sha=SOURCE)
                      if r.get("pattern")==OWNER and r.get("command")=="test"]
    assert 0 < len(candidates) <= 25, len(candidates)
    rows = []
    for candidate in candidates:
        iid = candidate["invocationId"]
        if not re.fullmatch(r"[0-9a-f-]{36}", iid) or candidate.get("commitSha") not in (None,"",SOURCE):
            continue
        [meta] = records("Invocation", invocation_id=iid)
        if meta["id"]["invocationId"] != iid or meta.get("commitSha") not in (None,"",SOURCE) or not meta.get("success") or meta.get("command")!="test":
            continue
        target = [t for t in records("Target", invocation_id=iid, label=OWNER)
                  if t.get("label")==OWNER and t.get("status")=="PASSED"]
        if len(target)!=1:
            continue
        [t] = target
        start = datetime.fromisoformat(t["timing"]["startTime"].replace("Z","+00:00"))
        if not "2026-10-29T10:25" <= start.isoformat() < "2026-10-29T10:32":
            continue
        for a in records("Action", invocation_id=iid, target_label=OWNER):
            if a.get("targetLabel")!=OWNER or a["id"]["invocationId"]!=iid:
                continue
            files = [{"name":f["name"],"uri":f["uri"],**({"digest":f["hash"]} if f.get("hash") else {}),
                      **({"length":f["sizeBytes"]} if "sizeBytes" in f else {})}
                     for f in a.get("file",[]) if Path(f["name"]).name in ("test.log","test.xml","outputs.zip")]
            if len(files)!=3:
                continue
            try:
                for f in files:
                    filename = Path(f["name"]).name
                    verified_file(f, out / ("profiles.zip" if filename=="outputs.zip" else filename))
            except AssertionError:
                continue
            finish_ms=round(start.timestamp()*1000)+int(Decimal(t["timing"]["duration"].removesuffix("s"))*1000)
            rows.append((iid,files,finish_ms,{"invocation":iid,"source_if_reported":meta.get("commitSha"),"target":t["id"],"action":a["id"],"timing":t["timing"],"status":t["status"],"files":files}))
    assert len(rows)==1, (len(candidates),len(rows))
    iid, files, finish, brief = rows[0]
    (out/"native.json").write_text(json.dumps(brief,indent=2)+"\n")
    verify(temp,out,iid,files,finish)


if __name__=="__main__":
    main(Path(sys.argv[1]))
