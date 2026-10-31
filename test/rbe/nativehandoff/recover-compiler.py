"""Recover declared compiler commands for the original release qualification."""
import datetime
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

service = "https://app.buildbuddy.io"
primary = "7f106d60-5f1e-4982-bb48-b92c6ad89cdc"
target = "//runsc/prewarmer:prewarmer_binary"
key = os.environ["BUILDBUDDY_API_KEY"]
dest = Path(os.environ["RUNNER_TEMP"])/"compiler-recovery"
dest.mkdir()
report = {"original_invocation": primary, "original_run": 41179358011, "original_artifact": 13035026611, "status": "INCOMPLETE", "witnesses": []}
def rpc(path, payload):
    req = urllib.request.Request(service+path, json.dumps(payload).encode(), {"Content-Type":"application/json","x-buildbuddy-api-key":key})
    with urllib.request.urlopen(req, timeout=45) as f:
        return json.load(f)
def blob(iid, digest):
    size = int(digest.get("sizeBytes",digest.get("size_bytes",0)))
    h = digest["hash"]
    assert re.fullmatch("[0-9a-f]{64}",h) and 0<size<2000000
    url = service+"/file/download?"+urllib.parse.urlencode({"bytestream_url":f"bytestream://remote.buildbuddy.io/blobs/{h}/{size}", "invocation_id":iid})
    # Keep the credential on the provider origin only.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args): return None
    req = urllib.request.Request(url,headers={"x-buildbuddy-api-key":key})
    with urllib.request.build_opener(NoRedirect).open(req,timeout=25) as f: raw=f.read(size+1)
    assert len(raw)==size and hashlib.sha256(raw).hexdigest()==h
    return raw
def fields(data):
    # Only length-delimited and varint fields occur in Action, Digest and Command.
    pos=0
    def varint():
        nonlocal pos
        n=0
        for shift in range(0,70,7):
            byte=data[pos]; pos+=1; n|=(byte&127)<<shift
            if byte<128: return n
        raise ValueError("invalid varint")
    while pos<len(data):
        tag=varint()
        if tag%8==0: value=varint()
        elif tag%8==2:
            length=varint(); value=data[pos:pos+length]; pos+=length
            assert len(value)==length
        else: raise ValueError("unexpected protobuf wire type")
        yield tag//8,value
def verify():
    # The release invocation identifies the unique runner that performed the four
    # earlier witness builds. Only read that runner's prewarmer actions.
    known=rpc("/api/v1/GetInvocation",{"selector":{"invocation_id":primary}})["invocation"][0]
    host=known["host"]
    assert host
    result=rpc("/rpc/BuildBuddyService/SearchInvocation",{"query":{"host":host,"pattern":target,"updatedAfter":"2026-10-31T07:07:46Z","updatedBefore":"2026-10-31T07:13:19Z"},"count":10})
    rows=result.get("invocation",[])
    assert len(rows)==3, f"expected three earlier prewarmer invocations; got {len(rows)}"
    arches=set()
    for row in rows:
        iid=row.get("invocationId") or row["invocation_id"]
        assert row["host"]==host
        a=rpc("/rpc/BuildBuddyService/GetExecution",{"executionLookup":{"invocationId":iid,"targetLabel":target}})["execution"]
        # An execution can have several attempts; only the completed result counts.
        completed=[e for e in a if e.get("stage") in (4,"COMPLETED")]
        assert len(completed)==1,len(completed)
        e,=completed
        digest=e.get("actionDigest") or e["action_digest"]
        cmd=dict(fields(blob(iid,digest)))[1]
        parts=dict(fields(cmd))
        args=[v.decode() for tag,v in fields(blob(iid,{"hash":parts[1].decode(),"sizeBytes":parts[2]})) if tag==1]
        command=" ".join(args)
        assert "prewarmer.c" in command
        # Persist only paths to the actual compile/link tools, never raw arguments.
        paths=re.findall(r"(?:external/|/usr/bin/)[a-zA-Z0-9_./+~@-]+",command)
        tools=[p for p in paths if "/bin/" in p and ("clang" in p or "llvm" in p or p.endswith("gcc"))]
        witness={"invocation":iid,"action_digest":digest["hash"],"tools":sorted(set(tools)),"exit":int(e.get("exitCode",e.get("exit_code",0)))}
        report["witnesses"].append(witness)
        if witness["exit"]==0:
            assert any("llvm" in p and "clang" in p for p in tools), tools
            assert any("llvm" in p and "ld.lld" in p for p in tools), tools
            architecture="aarch64" if "aarch64" in command else "x86_64" if "x86_64" in command else ""
            assert architecture
            witness["target"]=architecture
            arches.add(architecture)
        else:
            assert "aarch64-linux-gnu-gcc" in command
    assert arches=={"x86_64","aarch64"}
    report["status"]="PASS"
try: verify()
except Exception as exc:
    # Only a numeric HTTP status, or one of our narrow checks, can reach the report.
    report["reason"]=str(exc)[:360] if isinstance(exc, (AssertionError,KeyError)) else type(exc).__name__+":"+str(getattr(exc,"code",""))
finally:
    report["verified_at"]=datetime.datetime.now(datetime.timezone.utc).isoformat()
    (dest/"verdict.json").write_text(json.dumps(report,indent=2)+"\n")
print(report["status"])
raise SystemExit(report["status"]!="PASS")
