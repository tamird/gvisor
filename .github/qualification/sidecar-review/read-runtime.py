"""Read the sealed original archive and the two original executions only."""
import hashlib
import http.client
import io
import json
import os
import re
from pathlib import Path
import tarfile
import traceback
from xml.etree import ElementTree

ARCHIVE = ("ce8a162fd07a1f04adf5151930deb3dda68fb9f096d2e727f95748573c9846dc", 1151137)
PARENT = "e2fc1719-5024-41e7-aa8b-7874a57ee798"
ARMS = {"gnu-tests": "02ffd8f9-f490-486e-9033-efdd1ecd1914", "clang-tests": "537e792f-01e1-422b-9e48-fddfea868195"}
TESTS = {"//runsc/fdparking:fdparking_test": {"TestParking","TestParkingBinaryLayout","TestParkingMemoryFootprint"},
         "//runsc/prewarmer:prewarmer_single_page_test": {"TestPrewarmerFitsInOnePage"},
         "//runsc/procumounter:procumounter_test": {"TestUmounterFitsInOnePage","TestUmounterIsStatic","TestUmounter"}}
sha = lambda b: hashlib.sha256(b).hexdigest()

def read(key, path, body, limit):
    assert path in ("/api/v1/GetFile", "/rpc/BuildBuddyService/GetExecution")
    c = http.client.HTTPSConnection("app.buildbuddy.io",timeout=40)
    try:
        c.request("POST",path,json.dumps(body),{"content-type":"application/json","x-buildbuddy-api-key":key})
        with c.getresponse() as r:
            if r.status!=200: raise RuntimeError("HTTP_"+str(r.status))
            data=r.read(limit+1);assert len(data)<=limit
            return data
    finally:
        c.close()

def execution(key, child, lookup, inline):
    assert child in ARMS.values()
    data=read(key,"/rpc/BuildBuddyService/GetExecution",{"executionLookup":{"invocationId":child,**lookup},"inlineExecuteResponse":inline},24<<20)
    return json.loads(data).get("execution", [])

def match(row, facts):
    result = row.get("executeResponse",{}).get("result",{})
    outputs=result.get("outputFiles",[])
    matches={}
    for name in ("test.xml","test.log"):
        h,n=facts[name]
        hits=[o for o in outputs if o.get("path","").rsplit("/",1)[-1]==name and o.get("digest",{}).get("hash")==h and int(o["digest"].get("sizeBytes",0))==n]
        if len(hits)!=1:return None
        path=hits[0]["path"]
        assert re.fullmatch(r"bazel-out/[A-Za-z0-9._+/-]+/testlogs/[A-Za-z0-9._+/-]{1,240}",path)
        matches[name] = path
    assert row.get("stage") in ("COMPLETED",4) and int(row.get("exitCode",0))==0 and int(result.get("exitCode",0))==0
    assert int(row.get("status",{}).get("code",0))==0 and int(row.get("executeResponse",{}).get("status",{}).get("code",0))==0
    assert not row.get("executeResponse",{}).get("cachedResult",False)
    digest=row["actionDigest"]["hash"]
    assert len(digest)==64 and all(x in "0123456789abcdef" for x in digest)
    return {"action":digest,"worker_reports":matches,"worker":bool(result.get("executionMetadata",{}).get("worker") or row.get("executedActionMetadata",{}).get("worker") or row.get("executorHostname")),
            "test_setup":"test-setup.sh" in row.get("commandSnippet",""),"exit":int(result.get("exitCode",0))}

def main():
    key=os.environ.pop("BUILDBUDDY_API_KEY")
    out={"parent":PARENT,"archive":ARCHIVE,"arms":{}}
    try:
        raw=read(key,"/api/v1/GetFile",{"uri":"bytestream://remote.buildbuddy.io/blobs/%s/%d"%ARCHIVE},ARCHIVE[1])
        assert (sha(raw),len(raw))==ARCHIVE
        with tarfile.open(fileobj=io.BytesIO(raw)) as f:
            files={m.name.removeprefix("./"):m for m in f.getmembers() if m.isfile()};assert len(files)<2048
            def file(suffix):
                [m]=[v for k,v in files.items() if k==suffix or k.endswith("/"+suffix)]
                assert m.size<16<<20;return f.extractfile(m).read()
            for phase,child in ARMS.items():
                prefix="invocation-"+phase+"/";arm=out["arms"][phase]={"invocation":child,"owners":{}}
                events=[json.loads(l) for l in file(prefix+"events.jsonl").splitlines()]
                assert [e["started"]["uuid"] for e in events if "started" in e]==[child]
                assert [int(e["finished"]["exitCode"].get("code",0)) for e in events if "finished" in e]==[0]
                assert int(file(prefix+"native-exit"))==0;arm["exit"]=0
                reports=json.loads(file(prefix+"events.jsonl.manifest.json"))["reports"]
                for label,names in TESTS.items():
                    rec=arm["owners"][label]={}
                    summary=[e["testSummary"] for e in events if e.get("id",{}).get("testSummary",{}).get("label")==label and "testSummary" in e]
                    assert len(summary)==1 and summary[0].get("overallStatus")=="PASSED"
                    facts={}
                    for name in ("test.xml","test.log"):
                        [item]=[r for r in reports if r["identity"]["label"]==label and r["name"]==name]
                        data=file(prefix+item["path"]);assert (sha(data),len(data))==(item["sha256"],item["bytes"])
                        facts[name]=(sha(data),len(data))
                        if name=="test.xml":
                            nodes=ElementTree.fromstring(data).findall(".//testcase")
                            got=[n.get("name") for n in nodes]
                            counts={x:sum(n.find(x) is not None for n in nodes) for x in ("error","failure","skipped")}
                            rec.update(cases=len(got),named=sorted(names.intersection(got)),counts=counts,passed=names<=set(got) and not any(counts.values()))
                    rec["reports"]=facts
                    # RE file paths are execroot paths; the reporter's storage path is not.
                    candidates=[];rec["attempts"]=[]
                    try:
                        for actual in (label,label+"_amd64"):
                            rows=execution(key,child,{"targetLabel":actual},True)
                            rec["attempts"].append({"target":actual,"executions":len(rows),"with_result":sum(bool(r.get("executeResponse",{}).get("result")) for r in rows),"with_test_setup":sum("test-setup.sh" in r.get("commandSnippet","") for r in rows)})
                            candidates.extend(r for r in rows if match(r,facts))
                            if candidates:break
                        if not candidates:
                            # Some execution backends omit target_label. Only read the corresponding
                            # named test action; never fetch binaries or other action inputs.
                            summary=execution(key,child,{},False)
                            ids={r["actionDigest"]["hash"] for r in summary if "test-setup.sh" in r.get("commandSnippet","") and label.rsplit(":",1)[1] in r.get("commandSnippet","")}
                            rec["attempts"].append({"invocation_metadata":len(summary),"matching_test_setup":len(ids)})
                            assert len(ids)<=4
                            for h in ids:
                                candidates.extend(r for r in execution(key,child,{"actionDigestHash":h},True) if match(r,facts))
                        [r]=candidates
                        rec["physical"]=match(r,facts)
                    except Exception as e:
                        rec["metadata_error"]=str(e) if isinstance(e,RuntimeError) and str(e).startswith("HTTP_") else type(e).__name__
                        rec["metadata_lines"]=[t.lineno for t in traceback.extract_tb(e.__traceback__) if t.filename.endswith("read-runtime.py")][-3:]
                arm["passing_cases"]=sum(r["cases"]-sum(r["counts"].values()) for r in arm["owners"].values())
        assert all(r.get("passed") and r.get("physical",{}).get("worker") and r.get("physical",{}).get("test_setup") for a in out["arms"].values() for r in a["owners"].values())
        out["status"]="VERIFIED_ORIGINAL_RUNTIME"
    except Exception as e:
        out["status"]="INCOMPLETE";out["error"]=type(e).__name__
        out["lines"]=[t.lineno for t in traceback.extract_tb(e.__traceback__) if t.filename.endswith("read-runtime.py")][-4:]
    Path("verdict.json").write_text(json.dumps(out,indent=2)+"\n")
    return int(out["status"]!="VERIFIED_ORIGINAL_RUNTIME")

if __name__=="__main__":raise SystemExit(main())
