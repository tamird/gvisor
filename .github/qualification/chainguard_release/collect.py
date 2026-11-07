"""Check both original public release archives and the workers that wrote them."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile

W = Path(os.environ["RUNNER_TEMP"]) / "chainguard-release"
SOURCE = "bb983b988802b854bd03dd7a0a5220e7e7b4440e"
IMAGE = "docker://cgr.dev/chainguard/static@sha256:fc42af33e643f20d08baf03cddcc1c02ba57052dfb02edb0d79f3fc47bcea8d9"
ARCH = {"amd64":62, "arm64":183}
PACKAGE = "//test/rbe/release_prewarmer:"
TARS = {"//debian:gvisor-release-tar-bz2","//debian:gvisor-bin-tar"}
GO = {"//runsc:runsc","//runsc/cmd/sentry:gvisor_sentry"}
MEMBERS = {"runsc","containerd-shim-runsc-v1"} | {"gvisor-bin/"+x for x in
    ("gvisor_sentry","checkpointgofer","gvisor-sentry-prewarmer","runsc-fd-parking","runsc-metric-server","runsc-netgofer","gvisor-proc-umounter")}
BASE = json.loads((W/"baselines.json").read_text())
ROW = {"source":SOURCE, "version":(W/"version").read_text().strip(), "image":IMAGE,
       "exit":int((W/"exit").read_text()),"arms":{a:{} for a in ARCH},"firstFailures":[],
       "writers":[],"links":[],"verified":False}
TERMS = ("no such file or directory","file not found","command not found","permission denied","no space left","exec format error",
         "bad interpreter","no module named","libgcc_s.so.1","error while loading shared libraries","undeclared inclusion",
         "no registered executors","failed to pull","pull image","unavailable","deadline exceeded","out of memory","oom",
         "not executable","no such package","no matching toolchain","no matching execution platform","conflicting actions",
         "not a valid","requires a single","failed to fetch","cannot convert","invalid configuration","unknown option",
         "does not exist","syntax error","starlark","undefined reference","failed precondition","unauthorized",
         "tarfile","unresolved symlink","/bin/sh","/bin/bash","/usr/bin/env","/usr/bin/python","/bin/cp","/usr/bin/gcc",
         "/usr/bin/ld","/tmp","git","uname","clang","python","bootstrap","cpio","gzip","gawk","busybox","bzip2","build_tar")
def save():
    (W/"result.json").write_text(json.dumps(ROW,indent=2,sort_keys=True)+"\n")
def markers(text):
    return sorted(t for t in TERMS if t in text.lower())
def enum(s):
    return s if isinstance(s,str) and re.fullmatch(r"[a-zA-Z][\w-]{0,85}",s) else "other"
def label(s):
    return s if isinstance(s,str) and re.fullmatch(r"(?:@@?[\w+~._-]{1,120})?//[\w./+-]{0,180}:[\w./+-]{1,160}",s) else "other"
def blob(f):
    m=re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/(\d+)$",(f or {}).get("uri",""))
    return [m[1],int(m[2])] if m else None
def filehash(p):
    with p.open("rb") as f:return hashlib.file_digest(f,"sha256").hexdigest()


def bep():
    sets={};roots={}
    with (W/"build.bep").open() as lines:
        for line in lines:
            e=json.loads(line);i=e["id"];u=e.get("started",{}).get("uuid","")
            if re.fullmatch(r"[0-9a-f-]{36}",u):ROW["invocation"]=u
            # The event id and payload use distinct protobuf field names.
            if "namedSet" in i and "namedSetOfFiles" in e:sets[i["namedSet"]["id"]]=e["namedSetOfFiles"]
            t=i.get("targetCompleted",{}); owner=t.get("label")
            for a in ARCH:
                if owner==PACKAGE+"release_"+a and not t.get("aspect"):
                    c=e.get("completed",{});ROW["arms"][a]["built"]=bool(c.get("success"))
                    roots[a]=[x["id"] for g in c.get("outputGroup",[]) if g["name"]=="default" for x in g.get("fileSets",[])]
            act=e.get("action",{});ab=e.get("aborted",{})
            if ((act and not act.get("success")) or ab) and len(ROW["firstFailures"])<12:
                k=i.get("actionCompleted",{});d=act.get("failureDetail",{})
                ROW["firstFailures"].append({"owner":label(k.get("label",owner)),"type":enum(act.get("type")),
                    "exit":act.get("exitCode"),"terms":markers(d.get("message","")+" "+ab.get("description","")),
                    "code":enum(d.get("spawn",{}).get("code")),"stderr":blob(act.get("stderr")),"stdout":blob(act.get("stdout"))})
            if act and not act.get("success") and "firstOriginalActionFailure" not in ROW:
                k=i.get("actionCompleted",{});d=act.get("failureDetail",{})
                ROW["firstOriginalActionFailure"]={"owner":label(k.get("label")),"type":enum(act.get("type")),
                    "exit":act.get("exitCode"),"terms":markers(d.get("message","")),
                    "code":enum(d.get("spawn",{}).get("code")),"stderr":blob(act.get("stderr")),"stdout":blob(act.get("stdout"))}
            if "finished" in e:ROW["bazelExit"]=e["finished"]["exitCode"].get("code",0)
    for a,ids in roots.items():
        seen=set(); refs={}
        while ids:
            k=ids.pop()
            if k in seen:continue
            seen.add(k);node=sets.get(k,{})
            ids.extend(x["id"] for x in node.get("fileSets",[]))
            for f in node.get("files",[]):
                n=Path(f.get("name","")).name
                for ext in ("tar.bz2","runsc","sentry"):
                    if n=="release_"+a+"."+ext:refs[ext]={"cas":blob(f),"symlink":bool(f.get("symlinkTargetPath"))}
        ROW["arms"][a]["declaredOutputs"]=refs


def spawns():
    q=r'''select(.spawn) | .spawn |
      [.targetLabel,.mnemonic,.runner,(.cacheHit//false),(.exitCode//0),
       ((.platform.properties//[])|map({key:.name,value:.value})|from_entries|[.Arch,."container-image",."workload-isolation-type"]),
       (if .mnemonic=="PackageTar" then (.args[0]//""|endswith("/pkg/private/tar/build_tar")) else null end)]'''
    counts=Counter();other=Counter();cache=Counter();local=Counter();fallback=Counter()
    with subprocess.Popen([W/"bb","print","--raw","--compact_execution_log="+str(W/"build.compact")],
                          stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as dec:
        with subprocess.Popen(["jq","-c",q],stdin=dec.stdout,stdout=subprocess.PIPE,text=True) as select:
            dec.stdout.close()
            for line in select.stdout:
                owner,mnem,runner,cached,rc,props,writer=json.loads(line);arch,img,iso=props
                a=arch if arch in ARCH else "unspecified"; match=img==IMAGE and iso=="oci"
                if cached:cache[a]+=1
                elif runner=="remote":(counts if match else other)[a]+=1
                else:
                    local[enum(runner)]+=1
                    if runner in ("local","linux-sandbox","processwrapper-sandbox","worker","sandboxed-worker") and mnem!="WorkspaceStatusAction":
                        fallback[enum(mnem)]+=1
                if rc and len(ROW["firstFailures"])<20:
                    ROW["firstFailures"].append({"owner":label(owner),"type":enum(mnem),"arch":a,"remote":runner=="remote","exit":rc,"image":match})
                important=(mnem=="PackageTar" and owner in TARS);link=(mnem=="GoLink" and owner in GO)
                if important or link:
                    group=ROW["writers" if important else "links"]
                    if len(group)<16:group.append({"owner":owner,"arch":a,"matched":match,"remote":runner=="remote",
                                                    "cached":bool(cached),"exit":rc,"rulesPkgWriter":writer})
            jstatus=select.wait()
        dstatus=dec.wait()
    ROW["execution"]={"decoder":dstatus,"jq":jstatus,"freshRequestedOCI":dict(counts),"freshOtherRemote":dict(other),
                      "cacheHits":dict(cache),"otherRunners":dict(local),"unexpectedLocal":dict(fallback)}


def inspect(a):
    row=ROW["arms"][a];base=BASE[a]
    row["baseline"]={k:base[k] for k in ("id","artifactSHA256","source")}
    if not row.get("built"):return
    stem=(W/"paired")/("release_"+a)
    paths={k:Path(str(stem)+"."+k) for k in ("tar.bz2","runsc","sentry")}
    row["present"]={k:p.is_file() for k,p in paths.items()}
    if not all(row["present"].values()):return
    p=paths["tar.bz2"];row["archive"]={"bytes":p.stat().st_size,"sha256":filehash(p)}
    row["sameArchiveAsBaseline"]=row["archive"]["sha256"]==base["archive"]
    files=row["files"]={}
    with tarfile.open(p,"r|bz2") as tar:
        for e in tar:
            n=e.name.removeprefix("./").rstrip("/")
            if e.isdir():
                assert n in ("",".","gvisor-bin")
                continue
            assert n in MEMBERS and n not in files and e.isreg() and (e.mode & 0o7777)==0o755 and 0<e.size<300<<20
            f=tar.extractfile(e);h=hashlib.sha256();head=f.read(64);h.update(head)
            assert head[:6]==b"\x7fELF\x02\x01" and len(head)==64
            for c in iter(lambda:f.read(1<<20),b""):h.update(c)
            files[n]={"sha256":h.hexdigest(),"bytes":e.size,"mode":e.mode&0o7777,"e_machine":int.from_bytes(head[18:20],"little")}
    row["sameFilesAsBaseline"]=sorted(n for n in files if files[n]["sha256"]==base["files"][n])
    row["direct"]={}
    for n,ext in (("runsc","runsc"),("gvisor-bin/gvisor_sentry","sentry")):
        pp=paths[ext];row["direct"][n]={"bytes":pp.stat().st_size,"sha256":filehash(pp)}
    writers={x["owner"] for x in ROW["writers"] if x["arch"]==a and x["matched"] and x["remote"] and
             not x["cached"] and x["exit"]==0 and x["rulesPkgWriter"]}
    row["verified"]=bool(set(files)==MEMBERS and all(v["e_machine"]==ARCH[a] for v in files.values()) and
        all(files[n]["bytes"]==v["bytes"] and files[n]["sha256"]==v["sha256"] for n,v in row["direct"].items()) and
        set(row.get("declaredOutputs",{}))==set(paths) and writers==TARS)


try:
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==SOURCE
    if (W/"build.log").is_file():
        text=(W/"build.log").read_text(errors="replace")
        ROW["consoleTerms"]=markers(text)
        pattern=r"(?i)(?:exec: |execvp\(|failed to exec(?:ute)? |cannot (?:execute|find) |bad interpreter: )[\x22\x27]?([A-Za-z0-9_+./-]{1,180})(?:[\x22\x27]|\)|:| )"
        ROW["missingExecutables"]=sorted({Path(v).name for v in re.findall(pattern,text)
                                          if re.fullmatch(r"[A-Za-z0-9_+.-]{1,55}",Path(v).name)})[:12]
    for name_,fn,p in (("bep",bep,"build.bep"),("compact",spawns,"build.compact")):
        try:
            if (W/p).is_file():fn()
        except (OSError,ValueError,KeyError,TypeError) as e:ROW[name_+"ReaderError"]=type(e).__name__
        save()
    for a in ARCH:
        try:inspect(a)
        except (OSError,AssertionError,ValueError,KeyError,tarfile.TarError) as e:ROW["arms"][a]["archiveError"]=type(e).__name__
        save()
    stats=ROW.get("execution",{})
    ROW["verified"]=bool(ROW["exit"]==ROW.get("bazelExit")==0 and stats.get("jq")==stats.get("decoder")==0 and
                         not stats.get("freshOtherRemote") and all(r.get("verified") for r in ROW["arms"].values()))
finally:save()
