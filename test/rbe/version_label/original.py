"""Read the completed Make invocation and verify its six original artifacts."""
import hashlib
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
from datetime import datetime, timezone
from urllib.parse import urlsplit


HOST = "app.buildbuddy.io"
SECRET = os.environ.pop("BUILDBUDDY_API_KEY")
def request(path, body, maximum, dest=None):
    allowed = {'/rpc/BuildBuddyService/SearchInvocation', '/rpc/BuildBuddyService/GetTarget',
               '/rpc/BuildBuddyService/GetInvocation', '/rpc/BuildBuddyService/GetExecution', '/api/v1/GetInvocation', '/api/v1/GetAction', '/api/v1/GetFile'}
    assert path in allowed
    con = HTTPSConnection(HOST,timeout=120)
    try:
        con.request('POST',path,json.dumps(body),{'Content-Type':'application/json','x-buildbuddy-api-key':SECRET})
        with con.getresponse() as response:
            if response.status != 200:
                raise RuntimeError(f'{path}: HTTP {response.status}')
            if dest is None:
                data = response.read(maximum + 1)
                assert len(data) <= maximum
                return json.loads(data)
            digest = hashlib.sha256()
            size = 0
            with dest.open('wb') as stream:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    assert size <= maximum
                    digest.update(chunk)
                    stream.write(chunk)
            return (digest.hexdigest(),size)
    finally:
        con.close()


def identity(uri):
    url=urlsplit(uri)
    assert url.scheme=='bytestream' and url.hostname=='remote.buildbuddy.io'
    match=re.fullmatch(r'(?:/[^.][^/]*)*/blobs/([a-f0-9]{64})/([0-9]+)',url.path)
    assert match
    return match[1],int(match[2])

SOURCE = "14f280cd18a9aa968e2fee3b041d8621d95ff87f"
LABEL = "release-20261029.0"
REL = "//debian:release-artifacts"
TARGETS = ("//tools/bazeldefs:version", "//sandboxexec/sandbox/python:sandbox_py", REL)
OWNERS = {"runsc.deb": "//debian:debian", "gvisor.tar.zstd": "//debian:gvisor-release-tar-zstd",
          "gvisor.tar.bz2": "//debian:gvisor-release-tar-bz2"}
TMP = Path(os.environ["RUNNER_TEMP"]) / "make-read"
OUT = TMP / "receipt"
log = {"source": SOURCE, "github_run": 40904608668, "native": [], "packages": [], "binaries": {}}


def search(label):
    # No other source/owner outside the actual run's wall interval can anchor a read.
    query = {"pattern":label, "command":"build", "updatedAfter":"2026-10-29T15:45:14Z", "updatedBefore":"2026-10-29T16:00:18Z"}
    try:
        r = request("/rpc/BuildBuddyService/SearchInvocation", {"query":dict(query,commitSha=SOURCE),"count":30},8<<20)
        rows = r.get("invocation",[])
        assert not r.get("nextPageToken")
    except RuntimeError:
        rows = []
    if not rows:
        catalog=request("/api/v1/GetInvocation",{"selector":{"commit_sha":SOURCE}},4<<20)
        rows=[dict(v,invocationId=v["id"]["invocationId"]) for v in catalog.get("invocation",[])
              if datetime.fromisoformat(query["updatedAfter"]).timestamp() <= int(v["createdAtUsec"]) / 1e6 < datetime.fromisoformat(query["updatedBefore"]).timestamp()]
    if not rows:
        r = request("/rpc/BuildBuddyService/SearchInvocation",{"query":query,"count":30},8<<20)
        assert not r.get("nextPageToken")
        rows = r.get("invocation",[])
    rows = [v for v in rows if v.get("pattern") in (label,[label]) and v.get("commitSha",SOURCE)==SOURCE]
    assert len(rows)==1, (label,"matching original invocations",len(rows))
    return rows[0]["invocationId"]


def native(label):
    iid=search(label)
    assert re.fullmatch(r"[0-9a-f-]{36}",iid)
    [r] = request("/rpc/BuildBuddyService/GetInvocation", {"lookup":{"invocationId":iid}},32<<20)["invocation"]
    assert r.get("success") and r["invocationId"]==iid and r["pattern"]==[label]
    assert r.get("command")=="build" and r.get("commitSha",SOURCE)==SOURCE
    obs={"id":iid,"label":label,"host":r["host"],"commit":r.get("commitSha"),"options":{}}
    allcmd = r.get("structuredCommandLine",[])
    for item in r.get("event",[]):
        ev = item.get("buildEvent",{})
        if "structuredCommandLine" in ev:
            allcmd.append(ev["structuredCommandLine"])
        if (v:=ev.get("started")):
            obs["start"]=v.get("startTime",v.get("startTimeMillis"))
        if (v:=ev.get("finished")):
            obs["finish"]=v.get("finishTime",v.get("finishTimeMillis"))
            obs["exit"]=v["exitCode"].get("code",0 if v["exitCode"]["name"]=="SUCCESS" else None)
    for cl in allcmd:
        for section in cl.get("sections",[]):
            for opt in section.get("optionList",{}).get("option",[]):
                name=opt.get("optionName")
                if name in ("embed_label","compilation_mode","stamp"):
                    obs["options"][name]=opt.get("optionValue")
    log["native"].append(obs)
    assert obs.get("exit")==0 and obs.get("finish"), (label,"native finish/exit")
    return iid


def target_outputs(iid):
    res=request("/rpc/BuildBuddyService/GetTarget",{"invocationId":iid,"targetLabel":REL},16<<20)
    groups=res.get("targetGroups",res.get("targetGroup",[]))
    files=[f for group in groups for tgt in group.get("targets",[]) if tgt["metadata"]["label"]==REL for f in tgt.get("files",[])]
    files+= [f for a in request("/api/v1/GetAction",{"selector":{"invocation_id":iid,"target_label":REL}},8<<20).get("action",[]) for f in a.get("file",[])]
    by={}
    for f in files:
        full="/".join([*f.get("pathPrefix",[]),f.get("name","")])
        m=re.search(r"(?:(?:^|/)debian/release-artifacts/|^)(x86_64|aarch64)/(runsc[.]deb|gvisor[.]tar[.](?:bz2|zstd))$",full)
        if m and (uri:=f.get("uri","")).startswith("bytestream:"):
            val=identity(uri)
            k=m.groups()
            assert k not in by or by[k]==val
            by[k]=val
    log["advertised"]={a+"/"+n:list(v) for (a,n),v in by.items()}
    # If top-level staged symlinks expose no CAS URI, use exact underlying
    # PackageTar/MakeDeb output records in this same release invocation.
    for name,owner in OWNERS.items():
        if all((arch,name) in by for arch in ("x86_64","aarch64")):
            continue
        r=request("/rpc/BuildBuddyService/GetExecution",{"executionLookup":{"invocationId":iid,"targetLabel":owner},"inlineExecuteResponse":True},32<<20)
        results={}
        for e in r.get("execution",[]):
            if e.get("targetLabel")!=owner or e.get("status",{}).get("code",0) or e.get("exitCode",0):
                continue
            ar=e.get("executeResponse",{}).get("result",{})
            for f in ar.get("outputFiles",[]):
                if not re.fullmatch(r"bazel-out/[a-zA-Z0-9_.-]+/bin/debian/"+re.escape(name),f.get("path","")):
                    continue
                h=f["digest"]["hash"]; n=int(f["digest"]["sizeBytes"])
                assert re.fullmatch(r"[a-f0-9]{64}",h) and 0<n<350_000_000
                results[h]=(n,f["path"])
        log.setdefault("producer_outputs",{})[name]=results
        assert len(results)==2,(owner,"distinct archive outputs",len(results))
        for h,(n,path) in results.items():
            dest=TMP/h
            assert request("/api/v1/GetFile",{"uri":f"bytestream://remote.buildbuddy.io/blobs/{h}/{n}"},n,dest)==(h,n)
            arch=architecture(dest,name)
            k=arch,name
            assert k not in by or by[k]==(h,n)
            by[k]=(h,n)
    return by


def architecture(path,name):
    if name=="runsc.deb":
        cpu=subprocess.check_output(["dpkg-deb","-f",str(path),"Architecture"],text=True).strip()
        return {"amd64":"x86_64","arm64":"aarch64"}[cpu]
    with (subprocess.Popen(["zstd","-q","-dc",str(path)],stdout=subprocess.PIPE) if name.endswith("zstd") else path.open("rb")) as src:
        with tarfile.open(fileobj=src.stdout if hasattr(src,"stdout") else src,mode="r|*" ) as tar:
            for m in tar:
                if m.name.removeprefix("./")=="runsc":
                    hdr=tar.extractfile(m).read(20)
                    assert hdr[:4]==b"\x7fELF"
                    return {62:"x86_64",183:"aarch64"}[int.from_bytes(hdr[18:20],"little")]
    raise ValueError("missing runsc")


def validate(by):
    assert len(by)==6
    for arch in ("x86_64","aarch64"):
        base=TMP/arch
        base.mkdir()
        for name in OWNERS:
            h,n=by[arch,name]; dst=base/name
            assert 0<n<350_000_000
            if (TMP/h).exists():
                (TMP/h).rename(dst)
            else:
                assert request("/api/v1/GetFile",{"uri":f"bytestream://remote.buildbuddy.io/blobs/{h}/{n}"},n,dst)==(h,n)
            log["packages"].append({"arch":arch,"name":name,"sha256":h,"bytes":n})
        fields={k:subprocess.check_output(["dpkg-deb","-f",str(base/"runsc.deb"),k],text=True).strip() for k in ("Package","Version","Architecture")}
        with tarfile.open(base/"gvisor.tar.bz2","r:bz2") as tar:
            m,=[m for m in tar if m.name.removeprefix("./")=="runsc"]
            exe=TMP/(arch+"-runsc")
            with exe.open("wb") as out: shutil.copyfileobj(tar.extractfile(m),out)
        exe.chmod(0o700)
        cmd=[str(exe),"--version"] if arch=="x86_64" else ["qemu-aarch64",str(exe),"--version"]
        version=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,timeout=30)
        lines=[x for x in version.stdout.splitlines() if re.fullmatch(r"runsc version [a-zA-Z0-9._+-]+",x)]
        log["binaries"][arch]={"deb":fields,"version_lines":lines,"exit":version.returncode}
    result=subprocess.run([os.sys.executable,str(Path(__file__).with_name("packages.py")),str(TMP),LABEL,str(OUT/"packages.json")],
                          stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    if result.returncode:
        log["comparison"]={"exit":result.returncode,"location":[int(x) for x in re.findall(r'packages[.]py", line (\d+)',result.stderr)]}
    assert not result.returncode,"package comparator rejected recovered outputs"
    assert all(v["exit"]==0 and v["version_lines"]==["runsc version "+LABEL] for v in log["binaries"].values())


def main():
    ids=[native(t) for t in TARGETS]
    assert len(set(ids))==3 and len(set(r["host"] for r in log["native"]))==1
    assert log["native"][-1]["options"].get("embed_label")==LABEL
    assert log["native"][-1]["options"].get("compilation_mode")=="opt"
    validate(target_outputs(ids[-1]))


if __name__=="__main__":
    OUT.mkdir(parents=True)
    try:
        main()
        log["verdict"]="PASS"
    except Exception as exc:
        log["verdict"]="FAIL"
        log["diagnostic"]=f"{type(exc).__name__}: {exc}"[:350]
        raise
    finally:
        log["observed_at"]=datetime.now(timezone.utc).isoformat()
        (OUT/"original.json").write_text(json.dumps(log,indent=2)+"\n")
