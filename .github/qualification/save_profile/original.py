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

METHODS = {"/rpc/BuildBuddyService/GetInvocation", "/rpc/BuildBuddyService/SearchInvocation", "/api/v1/GetInvocation",
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
            limit = (64 if method == "/rpc/BuildBuddyService/GetInvocation" else 8) << 20
            data = response.read(limit + 1)
            assert len(data) <= limit
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
    rows, clues = [], []
    allowed_heads = (None, "", SOURCE, "cafaf4d6b60b6ffed6d19d0882aad895d68a54bb")
    def record(row):
        clues.append(row)
        (out/"read-diagnostic.json").write_text(json.dumps(clues,indent=2)+"\n")
    for candidate in candidates:
        iid = candidate["invocationId"]
        assert re.fullmatch(r"[0-9a-f-]{36}", iid)
        [meta] = records("Invocation", invocation_id=iid)
        record({"invocation":iid, "recorded_head":meta.get("commitSha"),"command":meta.get("command"),"success":meta.get("success")})
        if meta["id"]["invocationId"] != iid or meta.get("commitSha") not in allowed_heads or not meta.get("success") or meta.get("command")!="test":
            continue
        res=get("/rpc/BuildBuddyService/GetInvocation",{"lookup":{"invocationId":iid}})
        [original]=res["invocation"]
        assert original["invocationId"]==iid
        source_events=[e["buildEvent"] for e in original.get("event",[]) if "buildEvent" in e]
        attempts=[e for e in source_events if e.get("id",{}).get("testResult",{}).get("label")==OWNER and "testResult" in e]
        record({"invocation":iid,"event_count":len(source_events),"owner_attempts":len(attempts)})
        if not attempts:
            # A lookup may omit streamed events; the public test action and target
            # still name their original URI/size-bearing files and test attempt.
            for a in records("Action", invocation_id=iid,target_label=OWNER):
                if a["id"]["invocationId"]!=iid or a.get("targetLabel")!=OWNER:
                    continue
                files=[{"name":f["name"],"uri":f["uri"],**({"digest":f["hash"]} if f.get("hash") else {}),
                        **({"length":f["sizeBytes"]} if "sizeBytes" in f else {})} for f in a.get("file",[]) if f.get("uri")]
                record({"invocation":iid,"action":a["id"],"projected_files":[f["name"][:128] for f in files]})
                if files:
                    attempts.append({"id":a["id"],"testResult":{"status":"PASSED","testActionOutput":files},"evidence":"public action+passed target"})
        target = [t for t in records("Target", invocation_id=iid, label=OWNER)
                  if t.get("label")==OWNER and t.get("status")=="PASSED"]
        if len(target)!=1:
            continue
        [t] = target
        start = datetime.fromisoformat(t["timing"]["startTime"].replace("Z","+00:00"))
        if not "2026-10-29T10:25" <= start.isoformat() < "2026-10-29T10:32":
            continue
        for e in attempts:
            tr=e["testResult"]
            files=tr.get("testActionOutput",[])
            record({"invocation":iid,"status":tr.get("status"),"cached_locally":tr.get("cachedLocally",False),"files":[f.get("name","")[:128] for f in files]})
            if tr.get("status")!="PASSED" or tr.get("cachedLocally"):
                continue
            selected = []
            try:
                for expected, name in (("test.log","test.log"),("test.xml","test.xml"),("outputs.zip","profiles.zip")):
                    [f]=[f for f in files if Path(f.get("name","")).name==expected]
                    verified_file(f,out/name)
                    selected.append(f)
            except (ValueError,AssertionError):
                continue
            finish_ms=round(start.timestamp()*1000)+int(Decimal(t["timing"]["duration"].removesuffix("s"))*1000)
            if "testAttemptStartMillisEpoch" in tr and "testAttemptDurationMillis" in tr:
                finish_ms=int(tr["testAttemptStartMillisEpoch"])+int(tr["testAttemptDurationMillis"])
            elif "testAttemptStart" in tr and "testAttemptDuration" in tr:
                parsed=datetime.fromisoformat(tr["testAttemptStart"].replace("Z","+00:00"))
                finish_ms=round(parsed.timestamp()*1000)+int(Decimal(tr["testAttemptDuration"].removesuffix("s"))*1000)
            brief={"invocation":iid, "source_if_reported":meta.get("commitSha"),"target":t["id"],"action":e["id"],"source_record":e.get("evidence","BuildEventStream.TestResult"),"timing":t["timing"],"status":t["status"],
                   "files":[{k:f[k] for k in ("name","uri","digest","length") if k in f} for f in selected]}
            rows.append((iid,selected,finish_ms,brief))
    assert len(rows)==1, (len(candidates),len(rows))
    iid, files, finish, brief = rows[0]
    (out/"native.json").write_text(json.dumps(brief,indent=2)+"\n")
    verify(temp,out,iid,files,finish)


if __name__=="__main__":
    main(Path(sys.argv[1]))
