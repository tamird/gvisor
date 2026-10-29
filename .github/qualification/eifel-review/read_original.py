"""Project source-location and known assertion facts from one existing Eifel comparison."""
from datetime import datetime, timezone
from hashlib import sha256
from http.client import HTTPSConnection
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID
import io
import json
import os
import re
import tarfile
import traceback
import xml.etree.ElementTree as ET
import zipfile



ORIGINAL = "53e597fd-ea98-4647-b438-91a93cfba481"
HOST = "cb548161f537465fda2576793e6d6cea59a32260"
SOURCE = "0758e73f2109efdf99a9bd93cf97a8735622206b"
def identity(data):
    return {"sha256":sha256(data).hexdigest(),"bytes":len(data)}
def read(key, method, body, limit):
    assert method in ("GetInvocation","GetFile")
    c=HTTPSConnection("app.buildbuddy.io",timeout=50)
    try:
        c.request("POST","/api/v1/"+method,json.dumps(body),{"Content-Type":"application/json","x-buildbuddy-api-key":key})
        with c.getresponse() as r:
            assert r.status==200
            b=r.read(limit+1);assert len(b)<=limit
            return b
    finally: c.close()
def read_archive(key):
    [inv]=json.loads(read(key,"GetInvocation",{"selector":{"invocation_id":ORIGINAL},"include_artifacts":True},8<<20))["invocation"]
    assert inv["id"]["invocationId"]==ORIGINAL and inv["commitSha"]==HOST
    [uri]=[a["uri"] for a in inv["artifacts"] if a.get("name")=="qualification.tar.gz"]
    u=urlsplit(uri)
    m=re.fullmatch(r".*/blobs/([a-f0-9]{64})/([1-9][0-9]*)",u.path)
    assert u.scheme=="bytestream" and u.netloc=="remote.buildbuddy.io" and m
    d,n=m.groups();n=int(n);assert (d,n)==("2094367fac1a9645682f2c36d0b020ecae8f1816c0f2015116fae7f323ac0520",441600)
    b=read(key,"GetFile",{"uri":uri},n)
    assert identity(b)=={"bytes":n,"sha256":d}
    return b
CONTROL="499d0dcec661f36b7a7eb9dce92dd23135be91ca"
CASE="TestDetectSpuriousRecoveryWithRTO"
LABEL="//pkg/tcpip/transport/tcp/test/e2e:tcp_sack_test"
PHASES={"prior-head-controls":(CONTROL,"594fc236d57f7db0428bc581b6d4015ff551873fa8d38dc8ee2e95a437cabd21"),"corrected-sack":(SOURCE,"4e3b0d244400ee1fe26165dc5d5013777a37c002abfcbeb4e9ee54b9e73f89ed")}
MESSAGES=("RetransmitTS was not recorded","timed out waiting for the ACK probe","SpuriousRecovery = true, want false","SpuriousRecovery counter = 1, want 0","SpuriousRTORecovery counter = 1, want 0")
REGEX=(r"probe SndUna = (\d+), want ACK (\d+)",r"segment timestamp option = (true|false), want (true|false)",r"segment payload = (\d+) bytes, want (\d+)")
def observe(t,out):
    members=t.getmembers();assert len(members)==len({v.name for v in members})<=4096
    def get(n):
        m=t.getmember(n);assert m.isfile() and m.size<24<<20
        return t.extractfile(m).read()
    for phase,(source,digest) in PHASES.items():
        prefix="invocation-"+phase+"/"
        assert get(prefix+"head.txt").decode().strip()==source
        man=json.loads(get(prefix+"events.jsonl.manifest.json"))
        be=get(prefix+"events.jsonl");assert identity(be)=={k:man["events"][k] for k in ("bytes","sha256")}
        events=[json.loads(s) for s in be.splitlines()]
        [child]=[str(UUID(v["started"]["uuid"])) for v in events if v.get("started",{}).get("uuid")]
        [result]=[v for v in events if v.get("id",{}).get("testResult",{}).get("label")==LABEL and "testResult" in v]
        row=out[phase]={"source":source,"child":child,"xml_sha256":digest,"leaves":{}}
        reports={}
        for ref in man["reports"]:
            if ref["identity"]!=result["id"]["testResult"] or ref["name"] not in ("test.xml","test.log"):continue
            blob=get(prefix+ref["path"]);assert identity(blob)=={k:ref[k] for k in ("bytes","sha256")}
            assert ref["name"] not in reports;reports[ref["name"]]=blob
        assert identity(reports["test.xml"])["sha256"]==digest
        nodes=ET.fromstring(reports["test.xml"]).findall(".//testcase")
        for node in nodes:
            n=node.get("name")
            if n not in (CASE+"/missing_timestamp",CASE+"/zero_echo"):continue
            failure=node.find("failure");assert failure is not None
            s="".join(failure.itertext())
            lines=[]
            for line in s.splitlines():
                m=re.search(r"(?:^|\s)([a-zA-Z0-9_]+\.go):(\d+):\s*(.*)$",line)
                if not m or m[1] not in ("tcp_sack_test.go","context.go","endpoint.go","tcp_timestamp_test.go"):continue
                assert m[0].strip() in reports["test.log"].decode(errors="replace")
                exact=next((x for x in MESSAGES if x in m[3]),None)
                detail=next((mt.group(0) for p in REGEX if (mt:=re.search(p,m[3]))),None)
                fact={"file":m[1],"line":int(m[2]),"assertion":exact or detail or "other-source-assertion"}
                if fact not in lines:lines.append(fact)
            row["leaves"][n]=lines[:10]
        assert set(row["leaves"])=={CASE+"/missing_timestamp",CASE+"/zero_echo"} and all(row["leaves"].values())
def main():
    key=os.environ.pop("BUILDBUDDY_API_KEY")
    out={"original":ORIGINAL};rc=1
    try:
        b=read_archive(key);key=None;out["archive"]=identity(b)
        with tarfile.open(fileobj=io.BytesIO(b)) as t:observe(t,out)
        rc=0
    except Exception as exc:
        out["diagnostic_error"]=type(exc).__name__ if type(exc).__name__ in ("KeyError","AssertionError","ValueError","TypeError") else "OTHER"
        out["lines"]=[l.lineno for l in traceback.extract_tb(exc.__traceback__) if l.filename.endswith("read_original.py")][-4:]
    finally:
        key=None;Path("verdict.json").write_text(json.dumps(out,indent=2)+"\n")
    return rc
if __name__=="__main__":raise SystemExit(main())
