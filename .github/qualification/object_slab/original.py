"""Resolve the sole already-finished native owner; verify its original output URIs."""
from datetime import datetime
from decimal import Decimal
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
import sys

from verify import CONTROL as SOURCE, OWNER, verify, verified_file

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
            limit = 8 << 20
            data = response.read(limit + 1)
            assert len(data) <= limit
            return json.loads(data)
    finally:
        conn.close()


def records(method, **selector):
    return get("/api/v1/Get" + method, {"selector": selector}).get(method.lower(), [])


def main(temp):
    out = temp / "phase-results"
    carrier = "d5bad39277fda547548fe0134baed226c62ff531"
    allowed_heads = (None, "", SOURCE, carrier)
    candidates = {}
    for head in (SOURCE, carrier, None):
        q = dict(pattern=OWNER, command="test", updatedAfter="2026-10-29T14:10:30Z",
                 updatedBefore="2026-10-29T14:19:44Z")
        if head:
            q["commitSha"]=head
        try:
            page=get("/rpc/BuildBuddyService/SearchInvocation", {"query":q,"count":25})
            assert not page.get("nextPageToken")
            for candidate in page.get("invocation",[]):
                if candidate.get("commitSha") in allowed_heads:
                    candidates[candidate["invocationId"]]=candidate
        except ValueError:
            if not head:
                continue
            for candidate in records("Invocation", commit_sha=head):
                if candidate.get("pattern")==OWNER and candidate.get("command")=="test":
                    iid=candidate["id"]["invocationId"]
                    candidates[iid]=dict(candidate,invocationId=iid)
        if candidates:
            break
    if not candidates:
        for head in (SOURCE, carrier):
            for candidate in records("Invocation",commit_sha=head):
                if candidate.get("pattern")==OWNER and candidate.get("command")=="test":
                    iid=candidate["id"]["invocationId"]
                    candidates[iid]=dict(candidate,invocationId=iid)
    assert 0<len(candidates)<=25, len(candidates)
    rows, clues = [], []
    def record(row):
        clues.append(row)
        (out/"read-diagnostic.json").write_text(json.dumps(clues,indent=2)+"\n")
    for candidate in candidates.values():
        iid = candidate["invocationId"]
        assert re.fullmatch(r"[0-9a-f-]{36}", iid)
        [meta] = records("Invocation", invocation_id=iid)
        record({"invocation":iid, "recorded_head":meta.get("commitSha"),"command":meta.get("command"),"success":meta.get("success")})
        if meta["id"]["invocationId"] != iid or meta.get("commitSha") not in allowed_heads or not meta.get("success") or meta.get("command")!="test":
            continue
        attempts=[]
        for a in records("Action", invocation_id=iid,target_label=OWNER):
            if a["id"]["invocationId"]!=iid or a.get("targetLabel")!=OWNER:
                continue
            files=[{"name":f["name"],"uri":f["uri"],**({"digest":f["hash"]} if f.get("hash") else {}),
                    **({"length":f["sizeBytes"]} if "sizeBytes" in f else {})} for f in a.get("file",[]) if f.get("uri")]
            record({"invocation":iid,"action":a["id"],"projected_files":[f["name"][:128] for f in files]})
            if files:
                attempts.append({"id":a["id"],"files":files})
        target = [t for t in records("Target", invocation_id=iid, label=OWNER)
                  if t.get("label")==OWNER and t.get("status")=="PASSED"]
        if len(target)!=1:
            continue
        [t] = target
        start = datetime.fromisoformat(t["timing"]["startTime"].replace("Z","+00:00"))
        if not "2026-10-29T14:10" <= start.isoformat() < "2026-10-29T14:20":
            continue
        for e in attempts:
            files=e["files"]
            selected = []
            try:
                for expected, name in (("test.log","test.log"),("test.xml","test.xml"),("outputs.zip","profiles.zip")):
                    accepted = {expected, "test.outputs__outputs.zip"} if name == "profiles.zip" else {expected}
                    [f]=[f for f in files if Path(f.get("name","")).name in accepted]
                    verified_file(f,out/name)
                    selected.append(f)
            except (ValueError,AssertionError):
                continue
            finish_ms=round(start.timestamp()*1000)+int(Decimal(t["timing"]["duration"].removesuffix("s"))*1000)
            brief={"invocation":iid, "source_if_reported":meta.get("commitSha"),"target":t["id"],"action":e["id"],"source_record":"Public test action + passed target","timing":t["timing"],"status":t["status"],
                   "files":[{k:f[k] for k in ("name","uri","digest","length") if k in f} for f in selected]}
            rows.append((iid,selected,finish_ms,brief))
    assert len(rows)==1, (len(candidates),len(rows))
    iid, files, finish, brief = rows[0]
    (out/"native.json").write_text(json.dumps(brief,indent=2)+"\n")
    verify(temp,out,iid,files,{"targetFinishMillis":finish})


if __name__=="__main__":
    main(Path(sys.argv[1]))
