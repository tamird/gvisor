"""Read only the two original coverage builds and their recorded ELF files."""
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib import error, parse, request

WORK = Path(os.environ["RUNNER_TEMP"]) / "pr16507-coverage"
SOURCE = "7f56b080d9f7485957f3514133ffa5f5490a6185"
CARRIER = "71ab8953e0c25ba7cb0899a8f04786c33fd668ec"
BASE = "https://app.buildbuddy.io"
LABEL = "//runsc/cmd/sentry:"
NAMES = {
    LABEL+"gvisor_sentry": "gvisor_sentry", LABEL+"gvisor_sentry-race": "gvisor_sentry-race",
    LABEL+"gvisor_sentry_coverage": "gvisor_sentry_cov", LABEL+"gvisor_sentry_race_coverage": "gvisor_sentry_race_cov",
}
CLI = {
    "runsc-race": ("e09a4c610ea8b39b68216b2e9b1c90d6ca27a61962d83d918d45384be30b9d3d", 23034605),
    "runsc_race_coverage": ("8fbedd2c6c18b71067fff29a6da42be32a546557956bcf2820621ef4649ec1b0", 26361575),
}
ANCHOR = {"arm64": CLI["runsc-race"][0], "amd64": "c7d9af222925f0a48c90a9b6d78b3220849bf25a2500c763b785fc5fe997770e"}
MACHINE = {"amd64": 62, "arm64": 183}
UUID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")
DIGEST = re.compile(r"[0-9a-f]{64}")
report = {"source": SOURCE, "original_run": 42272993483, "cli": {}, "lookup": {}, "original": {}}


def save():
    data = json.dumps(report, indent=2).encode()
    assert len(data)<30000 and KEY.encode() not in data
    (WORK/"original.json").write_bytes(data)


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Incomplete(Exception):
    pass


def reason(exc):
    return "HTTP "+str(exc.code) if isinstance(exc, error.HTTPError) else type(exc).__name__


def value(data, camel, snake):
    return data.get(camel, data.get(snake))


def get(kind, message, limit):
    global calls, remaining
    calls += 1
    if calls>26 or remaining<limit:
        raise Incomplete("request limit")
    assert kind in {"api/v1/GetInvocation","api/v1/GetAction","api/v1/GetFile","rpc/BuildBuddyService/GetEventLogChunk"}
    req = request.Request(BASE+"/"+kind, data=json.dumps(message).encode(),
        headers={"Content-Type":"application/json","x-buildbuddy-api-key":KEY})
    with opener.open(req,timeout=90) as response:
        raw=response.read(limit+1)
    if len(raw)>limit:
        raise Incomplete("oversize")
    remaining -= len(raw)
    return raw


def api(kind, message, limit=2048*1024):
    return json.loads(get(kind, message, limit))


def summary(text):
    found={}
    for pattern in ("Resource Exhausted", "No space left", "No matching toolchains", "signal: killed",
                    "Killed", "build interrupted", "undeclared inclusion", "strict dependencies",
                    "undefined reference", "context deadline exceeded", "out of memory", "unavailable"):
        if re.search(re.escape(pattern),text,re.I):
            found[pattern]=True
    failures=[]
    for line in text.splitlines():
        if not re.search(r"ERROR:|failed \(Exit|failed: \(Exit|signal: killed",line,re.I):
            continue
        target=re.search(r"(//(?:runsc|pkg|tools|test)/[A-Za-z0-9_/.-]*:[A-Za-z0-9_+.@-]+)",line)
        mnemonic=re.search(r"\b(Go[a-zA-Z]+|Cpp[a-zA-Z]+|Genrule|action)\b",line)
        exit_code=re.search(r"\bExit (\d+)\b",line)
        if target and [target[1],None if not mnemonic else mnemonic[1],int(exit_code[1]) if exit_code else None] not in failures:
            failures.append([target[1],None if not mnemonic else mnemonic[1],int(exit_code[1]) if exit_code else None])
        if len(failures)==5:
            break
    return {"categories":sorted(found), "failed_targets":failures}


def elf(data, machine):
    return len(data)>64 and data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[16:18],"little") in (2,3) and int.from_bytes(data[18:20],"little")==machine


def blob(digest, size, machine):
    assert DIGEST.fullmatch(digest) and 0<size<=170*1024*1024
    raw=get("api/v1/GetFile",{"uri":"bytestream://remote.buildbuddy.io/blobs/"+digest+"/"+str(size)},size)
    if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=digest or not elf(raw,machine):
        raise Incomplete("invalid blob")
    return raw


def files(iid, target, filename):
    assert UUID.fullmatch(iid) and target in {*NAMES,"//runsc:runsc-race"}
    obj=api("api/v1/GetAction",{"selector":{"invocationId":iid,"targetLabel":target}})
    out=set()
    for act in obj.get("action",[]):
        if value(act,"targetLabel","target_label")!=target or value(act["id"],"invocationId","invocation_id")!=iid:
            continue
        for file in act.get("file",[]):
            if Path(file.get("name","")).name!=filename:
                continue
            m=re.fullmatch(r"bytestream://(?:remote|cache)\.buildbuddy\.io/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/(\d+)",file.get("uri",""))
            if m and 0<int(m[2])<=170*1024*1024:
                out.add((m[1],int(m[2])))
    return out


def selected():
    times={a:datetime(2026,11,6,h,m,tzinfo=timezone.utc).timestamp() for a,h,m in (("amd64",18,51),("arm64",19,31))}
    candidates={a:{} for a in times}
    for sha in (SOURCE,CARRIER):
        obj=api("api/v1/GetInvocation", {"selector":{"commitSha":sha}})
        for inv in obj.get("invocation",[]):
            if value(inv,"commitSha","commit_sha")!=sha or inv.get("command")!="build":
                continue
            repo=(value(inv,"repoUrl","repo_url") or "").removesuffix(".git")
            if repo not in ("","https://github.com/tamird/gvisor","git@github.com:tamird/gvisor"):
                continue
            pat=inv.get("pattern","")
            if "//runsc:runsc" not in pat:
                continue
            iid=value(inv.get("id",{}),"invocationId","invocation_id") or ""
            ts=int(value(inv,"createdAtUsec","created_at_usec") or 0)/1e6
            if not UUID.fullmatch(iid):
                continue
            for arch, start in times.items():
                if abs(ts-start)<110:
                    candidates[arch][iid]=all(k in pat for k in NAMES)
    return candidates


if __name__=="__main__":
    KEY=os.environ.pop("BUILDBUDDY_API_KEY")
    assert KEY
    calls,remaining=0,1250*1024*1024
    opener=request.build_opener(NoRedirect)
    save()
    try:
        candidates=selected()
        for arch, matches in candidates.items():
            if len(matches)!=1:
                report["lookup"][arch]={"matches":len(matches)}
                continue
            iid,full=next(iter(matches.items()))
            try:
                known=files(iid,"//runsc:runsc-race","runsc-race")
                if not full and not any(h==ANCHOR[arch] for h,_ in known):
                    report["lookup"][arch]={"matches":1,"unbound":True}
                    continue
                row={"invocation":iid,"complete_pattern":full,"cli_digest_bound":any(h==ANCHOR[arch] for h,_ in known),"targets":{}}
                report["original"][arch]=row
                for target, filename in NAMES.items():
                    try:
                        outputs=files(iid,target,filename)
                        r=row["targets"][target]={"output_count":len(outputs)}
                        if len(outputs)==1:
                            h,size=next(iter(outputs))
                            blob(h,size,MACHINE[arch])
                            r.update(sha256=h,size=size,machine=MACHINE[arch],elf=True)
                    except Exception as exc:
                        row["targets"].setdefault(target,{})["incomplete"]=reason(exc)
                log=api("rpc/BuildBuddyService/GetEventLogChunk",{"invocationId":iid,"minLines":400},4*1024*1024)
                row["last_log"]=summary(base64.b64decode(log.get("buffer","")).decode(errors="replace"))
            except Exception as exc:
                report["lookup"].setdefault(arch,{})["incomplete"]=reason(exc)
            save()
    except Exception as exc:
        report["lookup"]["incomplete"]=reason(exc)
    for name,(h,size) in CLI.items():
        row=report["cli"][name]={"sha256":h,"size":size}
        try:
            data=blob(h,size,183)
            path=WORK/name
            path.write_bytes(data)
            path.chmod(0o500)
            row.update(machine=183,verified=True)
        except Exception as exc:
            row["incomplete"]=reason(exc)
        save()
