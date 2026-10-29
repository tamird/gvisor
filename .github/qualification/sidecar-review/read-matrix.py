"""Diagnose only the original sidecar build and tests; never submit a Run."""
import hashlib, http.client, io, json, os, re, tarfile, traceback
from pathlib import Path
from xml.etree import ElementTree as ET

HASH="673e3d9710c7ffa5036e438411f19ec3a10e35a529774fdaf7e8ba88fe732628"
SIZE=1068460
PARENT="83a9be05-29d4-4075-ad78-bf9461ffcef6"
PHASES=("gnu-tests","clang-x86_64","clang-aarch64","clang-tests")
OWNERS={"fdparking_test","procumounter_test","prewarmer_single_page_test"}
sha=lambda b:hashlib.sha256(b).hexdigest()

def diagnostic(line,key):
    line=re.sub(r"\x1b\[[;\d]*[A-Za-z]","",line)
    if not re.search(r"(?i)(error:|not found|permission denied|failed to|no such|expected|got |want |fatal|undefined|cannot|unable|killed|invalid|exceeds|exec format|SIG[A-Z]+|^FAIL|[.]go:[0-9]+:|unsupported|unknown argument|requires|denied)",line):return None
    if len(line)>2000 or re.search(r"(?i)credential|password|secret|api.key|authorization|--remote.header|https?://|BEGIN [A-Z ]+ KEY|env - |build_event|--remote_.*=",line):return None
    line=re.sub(r"[A-Za-z0-9_+/=-]{48,}","[id]",line);line=re.sub(r"/[^ :]+/(?:external|execroot)/","[execroot]/",line)
    text="".join(c for c in line if 32<=ord(c)<127 or c=="\t")[:420]
    assert key not in text
    return text

def main():
    key=os.environ.pop("BUILDBUDDY_API_KEY");out={"invocation":PARENT,"archive":[HASH,SIZE],"phases":{}};status=1
    try:
        c=http.client.HTTPSConnection("app.buildbuddy.io",timeout=60)
        try:
            c.request("POST","/api/v1/GetFile",json.dumps({"uri":f"bytestream://remote.buildbuddy.io/blobs/{HASH}/{SIZE}"}),{"Content-Type":"application/json","x-buildbuddy-api-key":key})
            with c.getresponse() as r:
                assert r.status==200,r.status
                b=r.read(SIZE+1);assert len(b)==SIZE and sha(b)==HASH
        finally:c.close()
        with tarfile.open(fileobj=io.BytesIO(b)) as t:
            members=t.getmembers();assert len(members)<4096
            for phase in PHASES:
                row=out["phases"][phase]={"diagnostics":[],"tests":[]}
                for m in members:
                    f=m.name.removeprefix("./");name=f.rsplit("/",1)[-1]
                    if not m.isfile() or ("/invocation-"+phase+"/") not in ("/"+f) or name not in {"test.xml","test.log","stdout.log","stderr.log","native-exit","events.jsonl"}:continue
                    assert m.size<64<<20
                    raw=t.extractfile(m).read(); assert len(raw)==m.size
                    if name=="native-exit":row["exit"]=int(raw)
                    elif name=="events.jsonl":
                        for line in raw.splitlines():
                            e=json.loads(line);ident=e.get("id",{});summary=ident.get("testSummary",{})
                            if "started" in e:row["invocation"]=e["started"]["uuid"]
                            if "testSummary" in e and summary.get("label","").rsplit(":",1)[-1] in OWNERS:row.setdefault("outcomes",{})[summary["label"]]=e["testSummary"].get("overallStatus")
                            if "aborted" in e and len(row["diagnostics"])<24:
                                for reason in e["aborted"].get("description","").splitlines():
                                    q=diagnostic(reason,key)
                                    if q and q not in row["diagnostics"]:row["diagnostics"].append(q)
                    elif name=="test.xml":
                        nodes=ET.fromstring(raw).findall(".//testcase")
                        row["tests"].append({"sha256":sha(raw),"bytes":len(raw),"nodes":[[n.get("name"),"fail" if n.find("failure") is not None or n.find("error") is not None else "skip" if n.find("skipped") is not None else "pass"] for n in nodes if re.fullmatch("[A-Za-z0-9_/:.-]{1,160}",n.get("name",""))][:100]})
                    else:
                        for line in raw.decode(errors="replace").splitlines():
                            q=diagnostic(line,key)
                            if q and q not in row["diagnostics"] and len(row["diagnostics"])<35:row["diagnostics"].append(q)
                assert "exit" in row and "invocation" in row
        status=0
    except Exception as e:
        out["error"]=type(e).__name__;out["lines"]=[x.lineno for x in traceback.extract_tb(e.__traceback__) if x.filename.endswith("read-matrix.py")]
    Path("verdict.json").write_text(json.dumps(out,indent=2)+"\n")
    return status
if __name__=="__main__":raise SystemExit(main())
