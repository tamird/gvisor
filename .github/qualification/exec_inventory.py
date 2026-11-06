"""Map the release's configured Go execution tools to their original ELF files."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import urllib.error

import exec_cas as cas


BASE = "709cd18c362576aaea12aa6d36c72034dbe001c8"
OUT = Path(os.environ["RUNNER_TEMP"]) / "exec-inventory"
ROOTS = "deps(set(//runsc:runsc-race //runsc/cmd/sentry:gvisor_sentry-race))"
FLAGS = ["--config=aarch64","-c","opt","--config=race",
         "--platforms=@llvm//platforms:linux_aarch64_glibc_static",
         "--host_platform=//test/rbe/release_prewarmer:amd64",
         "--extra_execution_platforms=//test/rbe/release_prewarmer:amd64",
         "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash",
         "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
         "--lockfile_mode=off","--remote_executor=","--remote_cache=","--bes_backend=",
         "--curses=no","--color=no","--noshow_progress"]


def sane_path(path):
    p=PurePosixPath(path)
    return (not p.is_absolute() and ".." not in p.parts and len(path)<=450 and
            re.fullmatch(r"bazel-out/[A-Za-z0-9_.+-]*exec[A-Za-z0-9_.+-]*/bin/[a-zA-Z0-9_.@+/-]+",path))


def label(s):
    s=s[2:] if s.startswith("@@//") else s
    assert len(s)<340 and re.fullmatch(r"(?:@@?[A-Za-z0-9_.+-]+)?//[A-Za-z0-9_+./-]*:[A-Za-z0-9_+./-]+",s)
    return s


def aq(stem, expr, opts):
    path=OUT/(stem+".query")
    path.write_text(expr)
    with (OUT/(stem+".json")).open("wb") as dest,(OUT/(stem+".log")).open("wb") as diag:
        code=subprocess.run([str(OUT/"bazel"),"--nosystem_rc","--nohome_rc","aquery",*FLAGS,
            "--output=jsonproto","--query_file="+str(path),*opts],stdout=dest,stderr=diag,timeout=480).returncode
    assert code==0,stem
    return json.loads((OUT/(stem+".json")).read_bytes())


def graph():
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==BASE
    assert not subprocess.check_output(["git","diff","--name-only","HEAD"])
    assert hashlib.sha256(Path("test/rbe/release_prewarmer/BUILD").read_bytes()).hexdigest()=="ae1f23a11f3cb747fbec49a34c510c5ab9dcb75d06d94bfd4d9a49d50c629271"
    links=aq("links",'mnemonic("GoLink",'+ROOTS+')',["--include_artifacts","--noinclude_commandline"])
    frag={str(x["id"]):x for x in links["pathFragments"]}
    arts={str(x["id"]):str(x["pathFragmentId"]) for x in links["artifacts"]}
    def path_of(oid):
        parts=[]
        fid=arts[str(oid)]
        while fid not in ("0","None"):
            f=frag[fid]
            parts.append(f["label"])
            fid=str(f.get("parentId",0))
        return "/".join(reversed(parts))
    targets={str(t["id"]):t["label"] for t in links["targets"]}
    candidates={}
    for act in links["actions"]:
        for output in act.get("outputIds",[]):
            path=path_of(output)
            if sane_path(path):
                candidates[path]={"label":label(targets[str(act["targetId"])]),"direct":[],
                                  "arguments":[],"consumer_count":0}
    assert 0<len(candidates)<=90
    pattern="^("+"|".join(p.replace("+","[+]").replace(".","[.]") for p in candidates)+")$"
    q=aq("users",'inputs("'+pattern+'",'+ROOTS+')',["--noinclude_artifacts","--include_commandline"])
    targets={str(t["id"]):t["label"] for t in q["targets"]}
    for action in q["actions"]:
        argv=action.get("arguments",[])
        if not argv:
            continue
        for path in candidates.keys() & {a.split("=",1)[-1] for a in argv}:
            item=candidates[path]
            owner=label(targets[str(action["targetId"])])
            mnemonic=action["mnemonic"]
            assert re.fullmatch(r"[A-Za-z0-9_]{1,80}",mnemonic)
            row=[owner,mnemonic]
            key="direct" if argv[0]==path else "arguments"
            item["consumer_count"]+=1
            if row not in item[key] and len(item[key])<4:
                item[key].append(row)
    known={"//tools/go_generics/go_merge:go_merge","//tools/go_generics:go_generics","//tools/nogo:nogo",
           "//tools/go_marshal:go_marshal","//tools/go_stateify:stateify"}
    missing=sorted(known-{c["label"] for c in candidates.values() if c["direct"]})
    return {"source":BASE,"invocation":cas.INV,"candidates":candidates,"missing_controls":missing,
            "status":"PARTIAL" if missing else "PASS"}


def native(d):
    return cas.native(cas.blob(d,80*1024**2))


def audit(path, candidate):
    try:
        links=cas.executions(candidate["label"],"GoLink")
        for entry in links:
            response=entry.get("executeResponse",{})
            result=response.get("result",{})
            for file in result.get("outputFiles",[]):
                if file["path"]==path and not result.get("exitCode",0) and not response.get("status",{}).get("code",0):
                    return {"identity":"same-invocation-GoLink-exact-path","elf":native(file["digest"])}
    except (AssertionError,KeyError,ValueError,OSError) as exc:
        link_error=type(exc).__name__
    else:
        link_error="NoMatchingOutput"
    # Some GoLink results come from the action cache. A consumer's verified
    # input tree still contains the executable bytes that it was given.
    for consumer,mnemonic in candidate["direct"][:2]:
        try:
            for entry in cas.executions(consumer,mnemonic)[:6]:
                ac=dict(cas.fields(cas.blob(entry["actionDigest"],128*1024)))
                cmd=cas.blob(cas.digest_msg(ac[1]),5*1024*1024)
                if next(v for k,v in cas.fields(cmd) if k==1)!=path.encode():
                    continue
                parts=PurePosixPath(path).parts
                d=cas.digest_msg(ac[2])
                for i,part in enumerate(parts):
                    nodes=cas.fields(cas.blob(d,4*1024*1024))
                    node,=[m for k,v in nodes if k==(1 if i+1==len(parts) else 2) and
                           (m:=dict(cas.fields(v))).get(1)==part.encode()]
                    d=cas.digest_msg(node[2])
                return {"identity":"command-input","consumer":consumer,"mnemonic":mnemonic,"elf":native(d)}
        except (AssertionError,KeyError,IndexError,StopIteration,ValueError,OSError):
            continue
    return {"identity":"unavailable","link_recovery":link_error}


def inventory():
    r=json.loads((OUT/"graph-receipt.json").read_bytes())
    assert r["status"] in ("PASS","PARTIAL") and r["source"]==BASE and r["invocation"]==cas.INV and 0<len(r["candidates"])<=90
    public={"source":BASE,"invocation":cas.INV,"tools":{},"status":"INCOMPLETE"}
    for path,c in r["candidates"].items():
        assert sane_path(path) and label(c["label"])==c["label"]
        if not (c["direct"] or c["arguments"]):
            continue
        classification=audit(path,c)
        public["tools"][path]={"owner":c["label"],"direct_consumer":c["direct"][:1],
                               "argument_consumer":c["arguments"][:1],**classification}
        save("elf-receipt",public)
    public["status"]="PASS" if all(v.get("elf") for v in public["tools"].values()) else "PARTIAL"
    return public


def save(name,obj):
    raw=json.dumps(obj,indent=2).encode()
    assert len(raw)<256*1024 and (not os.environ.get("ACCESS") or os.environ["ACCESS"].encode() not in raw)
    (OUT/(name+".json")).write_bytes(raw)


mode,=sys.argv[1:]
assert mode in ("graph","elf")
result={"status":"INCOMPLETE","source":BASE,"invocation":cas.INV}
try:
    result=graph() if mode=="graph" else inventory()
except (OSError,AssertionError,KeyError,IndexError,ValueError,subprocess.SubprocessError) as exc:
    import traceback
    result.update(error=type(exc).__name__,line=traceback.extract_tb(exc.__traceback__)[-1].lineno)
save(mode+"-receipt",result)
print(mode,result["status"])
sys.exit(mode=="graph" and result["status"]=="INCOMPLETE")
