"""Resolve the generator and its execution platform on the same ARM test graph."""
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import sys
from urllib.error import HTTPError
from exec_cas import INV, blob, digest_msg, executions, fields, native
from probe import WORK, OUT, BASE, OWNER, PLATFORM, IMAGE, SOURCE, PUB, HEX, save, signals


FILTER = "//runsc/gofer/fs/filter:filter_precompiled"
TOOL = re.compile(r"filter_precompiled_gen_(?:stubbed|static|dynamic)_bin$")
QEMU = re.compile(r"qemu-(?:aarch64|x86_64)(?:-static)?$")
APATH = re.compile(r"(?:bazel-out|external)/[a-zA-Z0-9_.,~+/=@-]{1,500}")
ARCH = {62:"amd64",183:"arm64"}
AMD = "//tools/bazeldefs:linux_amd64"
BRANCHES = {"original", "host-arm64", "per-platform"}


def allowed(s):
    if not APATH.fullmatch(s) or ".." in PurePosixPath(s).parts: return None
    name=PurePosixPath(s).name
    return "generator" if TOOL.fullmatch(name) and "/runsc/gofer/fs/filter/" in s else "qemu" if QEMU.fullmatch(name) else None


def props(vals):
    info={}
    for k,v in vals.items():
        if k=="Arch" and v in ("amd64","arm64"): info["Arch"]=v
        elif k=="OSFamily" and v=="linux": info[k]=v
        elif k=="container-image":info["ubuntu_index"]=v==IMAGE
        elif k=="workload-isolation-type" and v in ("oci","firecracker"):info[k]=v
    return info


def list_props(data):
    dst={}
    for k,v in fields(data):
        if k==1:
            kv=dict(fields(v))
            if 1 in kv and 2 in kv: dst[kv[1].decode()]=kv[2].decode()
    return props(dst)


def elf_from_input(root, path):
    for i,part in enumerate(path.parts):
        nodes=[dict(fields(b)) for t,b in fields(blob(root,3*1024*1024)) if t==(1 if i+1==len(path.parts) else 2)]
        n,=[n for n in nodes if n.get(1)==part.encode()]
        root=digest_msg(n[2])
    return native(blob(root,35*1024*1024))


def original():
    data={"source":SOURCE,"invocation":INV,"owner":FILTER,"actions":[]}
    try:
        rows=executions(FILTER,"PrecompileSeccomp")
        assert 1<=len(rows)<=12
        for r in rows:
            ex=r.get("executeResponse",{}); status=ex.get("status") or r.get("status",{});out=ex.get("result",{})
            code=out.get("exitCode",0)
            if not code and not status.get("code",0): continue
            diag=base64.b64decode(out.get("stderrRaw",""))
            if not diag and int(out.get("stderrDigest",{}).get("sizeBytes",0)):
                diag=blob(out["stderrDigest"],512*1024)
            item={"code":code,"rpc":status.get("code",0),"diagnostics":signals(diag.decode(errors="replace")+" "+status.get("message","")),"inputs":{}}
            data["actions"].append(item)
            d=r["actionDigest"]
            ac=dict(fields(blob(d,64*1024)))
            item["action_sha256"]=d["hash"]
            c=list(fields(blob(digest_msg(ac[1]),4*1024*1024)))
            argv=[v.decode() for t,v in c if t==1]
            assert argv and allowed(argv[0])
            item["argv0"]=argv[0]
            item["qemu_invoked"]=allowed(argv[0])=="qemu"
            if 10 in ac: item["action_platform"]=list_props(ac[10])
            for tag,v in c:
                if tag==5: item["command_platform"]=list_props(v)
            for index,arg in enumerate(argv):
                role=allowed(arg)
                if not role or (index and role=="qemu"): continue
                obj=item["inputs"][role]={"path":arg}
                try:
                    obj["elf"]=elf_from_input(digest_msg(ac[2]),PurePosixPath(arg))
                    obj["identity"]="original-command-input"
                except (AssertionError,KeyError,ValueError,IndexError,HTTPError,OSError):
                    if role!="generator":continue
                    producer=FILTER.rsplit(":",1)[0]+":"+PurePosixPath(arg).name
                    links=executions(producer,"GoLink")
                    matches={f["digest"]["hash"]:f for e in links for f in e.get("executeResponse",{}).get("result",{}).get("outputFiles",[]) if f.get("path")==arg}
                    if len(matches)==1:
                        obj["elf"]=native(blob(next(iter(matches.values()))["digest"],35*1024*1024))
                        obj["identity"]="same-invocation-GoLink-exact-output"
                save("original",data)
    except (OSError,ValueError,KeyError,IndexError,AssertionError,struct.error) as e:
        data["incomplete"]=type(e).__name__
        if isinstance(e,HTTPError): data["http"]=e.code
    assert os.environ["ACCESS"] not in json.dumps(data)
    save("original",data)


def original_profile():
    return (WORK/"profile.bazelrc").read_text()


def configure(case):
    assert case in BRANCHES
    base=original_profile()
    if case=="host-arm64":
        base += "build:arm-source --host_platform="+PLATFORM+"\n"
    if case=="per-platform":
        # Keep one logical platform label per CPU. Compare architecture
        # properties on those same labels without changing the user's tree.
        p=Path("tools/bazeldefs/BUILD")
        src=subprocess.check_output(["git","show",SOURCE+":tools/bazeldefs/BUILD"],text=True)
        for cpu in ("amd64","arm64"):
            name='    name = "linux_'+cpu+'",\n'
            assert src.count(name)==1
            entries={"Arch":cpu,"OSFamily":"linux","container-image":IMAGE,"workload-isolation-type":"oci"}
            src=src.replace(name,name+"    exec_properties = "+json.dumps(entries,sort_keys=True)+",\n")
        p.write_text(src)
        base=base.replace("--extra_execution_platforms="+PLATFORM, "--extra_execution_platforms="+PLATFORM+","+AMD)
        base=base.replace("build:arm-source --remote_default_exec_properties=Arch=arm64\n","")
        base+="build:arm-source --host_platform="+AMD+"\n"
    return base


def exe(path):
    return path.replace("@@","") if path.replace("@@","") in (PLATFORM,AMD,"@platforms//host:host","@local_config_platform//:host","@bazel_tools//tools:host_platform") else "other"


def graph(case):
    p=Path("tools/bazeldefs/BUILD")
    old=p.read_bytes()
    rc=WORK/(case+".bazelrc")
    try:
        rc.write_text(configure(case))
        # Query the exact configured closure of the public service test;
        # another top-level configuration may resolve different generators.
        expr=('mnemonic("^PrecompileSeccomp$",deps('+OWNER+')) union mnemonic("^GoLink$",'
              'filter("runsc/gofer/fs/filter:filter_precompiled_gen_(stubbed|dynamic|static)_bin$",deps('+OWNER+'))) union '
              'mnemonic("^TestRunner$",'+OWNER+')')
        with (WORK/(case+".json")).open("w") as dst,(WORK/(case+".log")).open("w") as log:
            code=subprocess.run(["timeout","--signal=INT","--kill-after=30s","9m",*BASE,"--bazelrc="+str(rc),
                "aquery","--config=arm-source","--spawn_strategy=local","--strategy=TestRunner=local","--jobs=4",
                "--output=jsonproto","--include_artifacts=false","--include_commandline",expr],stdout=dst,stderr=log).returncode
        result={"source":SOURCE,"profile":case,"exit":code,"generators":[],"links":[],"tests":[]}
        if code:
            result["diagnostics"]=signals((WORK/(case+".log")).read_text(errors="replace"))
            return result
        d=json.loads((WORK/(case+".json")).read_text())
        targets={str(t["id"]):t["label"] for t in d["targets"]}
        for a in d["actions"]:
            owner=targets[str(a["targetId"])]
            platform=exe(a.get("executionPlatform",""))
            args=a.get("arguments",[])
            row={"owner":owner,"platform":platform}
            if owner==FILTER and a["mnemonic"]=="PrecompileSeccomp":
                row.update(argv0=args[0] if allowed(args[0]) else "other",generator=next((s for s in args if allowed(s)=="generator"),None))
                result["generators"].append(row)
            if owner.startswith(FILTER+"_gen_") and a["mnemonic"]=="GoLink":
                env={e["key"]:e.get("value","") for e in a.get("environmentVariables",[])}
                declared=env.get("GOARCH")
                if declared not in ("amd64","arm64"):
                    for i, s in enumerate(args[:-1]):
                        if s in ("-goarch","--goarch") and args[i+1] in ("amd64","arm64"):declared=args[i+1]
                outputs=[s for s in args if allowed(s)=="generator"]
                row.update(goarch=declared if declared in ("amd64","arm64") else None,outputs=outputs)
                result["links"].append(row)
            if owner==OWNER and a["mnemonic"]=="TestRunner":
                props0={p["key"]:p.get("value","") for p in a.get("executionInfo",[])}
                row["firecracker"]=props0.get("workload-isolation-type")=="firecracker"
                result["tests"].append(row)
        return result
    finally:
        if case=="per-platform":p.write_bytes(old)


def choose():
    orig=json.loads((OUT/"original.json").read_text())
    profiles={c:json.loads((OUT/(("original-graph" if c=="original" else c)+".json")).read_text()) for c in BRANCHES if (OUT/(("original-graph" if c=="original" else c)+".json")).exists()}
    # The conditional test keeps the product source exactly as published.
    # The per-platform profile is an independent analysis comparator.
    out={"source":SOURCE,"selected":None,"candidates":{},"original_mismatch":False}
    physical={}
    old_paths=[]
    for a in orig["actions"]:
        if not a.get("argv0"):continue
        for obj in a.get("inputs",{}).values():
            machine=ARCH.get(obj.get("elf",{}).get("machine"))
            if machine:physical[obj["path"]]=machine
        old_paths.append(a["argv0"])
        remote=a.get("action_platform",{}).get("Arch") or a.get("command_platform",{}).get("Arch")
        if a["argv0"] in physical and remote and physical[a["argv0"]]!=remote:
            out["original_mismatch"]=True
    out["original_argv0_configured"]=any(g["argv0"] in old_paths for g in profiles.get("original",{}).get("generators",[]))
    for key in ("host-arm64","per-platform"):
        profile=profiles.get(key,{})
        if profile.get("exit")!=0: continue
        drivers=profile.get("generators",[]); links=profile.get("links",[])
        routes=[]
        for g in drivers:
            cpu={PLATFORM:"arm64",AMD:"amd64"}.get(g["platform"])
            # In the host-only profile, all remote actions get Arch=arm64.
            remote="arm64" if key=="host-arm64" else cpu
            input_arch=physical.get(g["argv0"])
            if not input_arch and g["argv0"]==g["generator"]:
                input_arch=next((l["goarch"] for l in links if l.get("goarch") and g["generator"] in l["outputs"]),None)
            routes.append({"remote":remote,"exec_platform":cpu,"input_arch":input_arch,
                           "argv_changed":g["argv0"] not in old_paths})
        runner=any(t["platform"]==PLATFORM and t["firecracker"] for t in profile["tests"])
        # If the linker omits its environment from aquery, the actual test
        # can verify an otherwise changed route. An unchanged known mismatch
        # must never resubmit the previous failing execution.
        safe=bool(routes) and runner and all(r["remote"] and r["exec_platform"]==r["remote"] and (
            r["input_arch"]==r["remote"] or (not r["input_arch"] and r["argv_changed"])) for r in routes)
        out["candidates"][key]={"runner_ok":runner,"routes":routes,"credible":safe}
        if safe and not out["selected"] and out["original_argv0_configured"] and any(physical.values()):
            out["selected"]=key
    save("route",out)


if __name__=="__main__":
    mode=sys.argv[1]
    if mode=="original":original()
    elif mode=="choose":choose()
    elif mode=="activate":
        key=json.loads((OUT/"route.json").read_text())["selected"]
        assert key in {"host-arm64","per-platform"}
        (WORK/"profile.bazelrc").write_text(configure(key))
    else:save(mode,graph(mode))
