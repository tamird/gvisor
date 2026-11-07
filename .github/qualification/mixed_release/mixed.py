"""Qualify the production archive and Python distribution build in one invocation."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import traceback
import uuid
import zipfile

D=Path(os.environ["RUNNER_TEMP"])/"mixed-publication"
SOURCE="d6f54c947d3ddb46c773c82b5be10ee438d379fb"
STAMP="release-20261106.0"
VERSION="2026.10.26.13"
TARGET="//debian:release-inputs"
PY="//sandboxexec/sandbox/python:dist"
IMAGE="docker://cgr.dev/chainguard/static@sha256:fc42af33e643f20d08baf03cddcc1c02ba57052dfb02edb0d79f3fc47bcea8d9"
WORKER="//test/rbe/stamped_release:"
KINDS={"//debian:debian":("MakeDeb","runsc.deb"),"//debian:gvisor-release-tar-bz2_archive":("Tar","gvisor.tar.bz2"),
       "//debian:gvisor-release-tar-zstd_archive":("Tar","gvisor.tar.zstd"),PY:("SandboxExecDist","dist")}
STAGE=Path("bazel-bin/debian/release-inputs")
MANIFEST=STAGE/"inputs"
sys.path.insert(0,str(Path(__file__).resolve().parent))
from cold_repository import headlines
from native_archives import archive


def save(name,row):
    out=json.dumps(row,indent=2)+"\n"
    assert len(out)<100_000 and not re.search(r"(?i)x-buildbuddy-api-key|authorization:|github_pat_|gh[pousr]_",out)
    (D/"public"/(name+".json")).write_text(out)


def read(name):
    return json.loads((D/"public"/(name+".json")).read_text())


def blocks():
    txt=Path(".buildkite/release.yaml").read_text()
    step=txt.split('    label: ":ship: Release"\n',1)[1].split("    agents:\n",1)[0]
    chunks=[]
    for block in step.split("      - |\n")[1:]:
        command=[]
        for line in block.splitlines():
            if not line.startswith("        "): break
            command.append(line[8:])
        chunks.append("\n".join(command).replace("$$","$"))
    assert len(chunks)==1 and chunks[0].count("bazel build")==1 and chunks[0].endswith(TARGET)
    sign,=(line.strip()[2:].replace("$$","$") for line in step.splitlines() if line.strip().startswith("- NIGHTLY="))
    assert sign.endswith("tools/make_release.sh repo.key repo bazel-bin/debian/release-inputs")
    return chunks[0],sign


def flags():
    return ["--config=x86_64","--lockfile_mode=off","--jobs=48","--platform_mappings=test/rbe/stamped_release/platform_mappings",
            "--extra_execution_platforms="+",".join(WORKER+a for a in ("amd64","arm64")),
            "--extra_toolchains="+",".join("//test/rbe/go_bootstrap:linux_"+a+"_ash" for a in ("amd64","arm64")),
            "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
            "--@rules_python//python/config_settings:bootstrap_impl=native",
            "--modify_execution_info=^(MakeDeb|Tar|SandboxExecDist|ReleaseSHA512)$=+no-remote-cache","--color=no","--curses=no","--noshow_progress"]


def startup(remote=False):
    args=[str(D/"bazel.public"),"--nosystem_rc","--nohome_rc","--bazelrc="+str(D/"heap.rc")]
    return args+["--bazelrc="+str(D/"client.rc")] if remote else args


def build_args():
    return ["-c","opt","--config=release_version","--embed_label="+STAMP,
            "--//sandboxexec/sandbox/python:dist_version="+VERSION,"--remote_download_outputs=toplevel",TARGET]


def plan():
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==SOURCE and not (D/"client.rc").exists()
    blocks()
    base=startup()+["aquery",*flags(),*build_args()[:-1],"--remote_executor=","--remote_cache=","--bes_backend=",
                    "--output=jsonproto","--include_artifacts","--include_commandline"]
    p=subprocess.run(base+['mnemonic("^(MakeDeb|Tar|SandboxExecDist|ReleaseSHA512)$", deps('+TARGET+'))'],capture_output=True)
    doc={"source":SOURCE,"exit":p.returncode,"actions":[],"verified":False}
    if p.returncode:
        doc["diagnostic"]=headlines(p.stderr)
        save("graph",doc); return False
    g=json.loads(p.stdout)
    labels={str(t["id"]):t["label"].lstrip("@") for t in g["targets"]}
    configs={str(t["id"]):t for t in g["configuration"]}
    frags={str(t["id"]):t for t in g["pathFragments"]}
    art={str(t["id"]):str(t["pathFragmentId"]) for t in g["artifacts"]}
    for a in g["actions"]:
        owner=labels[str(a["targetId"])]
        is_sum=owner==TARGET and a["mnemonic"]=="ReleaseSHA512"
        if not is_sum and (owner not in KINDS or a["mnemonic"]!=KINDS[owner][0]): continue
        conf=configs[str(a["configurationId"])]
        cpu={"k8":"amd64","x86_64":"amd64","amd64":"amd64","arm64":"arm64","aarch64":"arm64"}[conf["mnemonic"].split("-")[0]]
        paths=[]
        for output in a["outputIds"]:
            parts=[]; node=art[str(output)]
            while node in frags:
                v=frags[node];parts.append(v["label"]);node=str(v.get("parentId",""))
            path="/".join(reversed(parts))
            if is_sum or path.endswith("/"+KINDS[owner][1]): paths.append(path)
        assert len(paths)==1
        doc["actions"].append({"owner":owner,"mnemonic":a["mnemonic"],"target":cpu,"worker":a["executionPlatform"].lstrip("@"),
                               "config":conf["checksum"],"path":paths[0],**({"args":a["arguments"]} if is_sum else {})})
    expect={(o,c) for o in KINDS for c in (("amd64",) if o==PY else ("amd64","arm64"))}
    old=[a for a in doc["actions"] if a["owner"]!=TARGET]
    sums=[a for a in doc["actions"] if a["owner"]==TARGET]
    origins={a["path"] for a in old if a["mnemonic"]!="MakeDeb"}
    doc["verified"]=(len(old)==len(expect) and {(a["owner"],a["target"]) for a in old}==expect and
            len(sums)==5 and {a["args"][-2] for a in sums}==origins and
            all(a["args"][-1]==a["path"] and a["args"][-3] in ("checksum","python") and a["target"]=="amd64" for a in sums) and
            all(a["worker"]==WORKER+a["target"] for a in doc["actions"]))
    save("graph",doc)
    return doc["verified"]


def build():
    assert read("graph")["verified"]
    iid=str(uuid.uuid4())
    meta={"source":SOURCE,"invocation":iid,"exit":None}; save("build",meta)
    wrappers=D/"bin";wrappers.mkdir();shim=wrappers/"bazel"
    args=startup(True)+["build",*flags(),"--keep_going","--invocation_id="+iid,"--build_event_publish_all_actions",
                        "--build_event_json_file="+str(D/"build.bep"),"--execution_log_compact_file="+str(D/"build.compact")]
    shim.write_text("#!/usr/bin/python3\nimport sys,os,pathlib,json\n"+
        "expected="+repr(["build",*build_args()])+"\nassert sys.argv[1:]==expected\n"+
        "pathlib.Path("+repr(str(D/"original-argv.json"))+").write_text(json.dumps(sys.argv[1:]))\n"+
        "real="+repr(args)+"\nos.execv(real[0],[*real,*sys.argv[2:]])\n")
    shim.chmod(0o700)
    env={**os.environ,"BUILDKITE_TAG":STAMP+"-staging","RELEASE_NAME":"release-20261026.13","PATH":str(wrappers)+":"+os.environ["PATH"]}
    with (D/"build.log").open("wb") as f:
        p=subprocess.run(["timeout","--signal=INT","--kill-after=30s","100m","bash","-euc",blocks()[0]],env=env,stdout=f,stderr=f)
    meta.update(exit=p.returncode,argv=json.loads((D/"original-argv.json").read_text()) if (D/"original-argv.json").is_file() else [])
    save("build",meta)
    return p.returncode==0

def boundary():
    assert not (D/"client.rc").exists()
    doc={"source":SOURCE,"verified":False};save("boundary",doc)
    opts=[a if not a.startswith("--//sandboxexec/sandbox/python:dist_version=") else "--//sandboxexec/sandbox/python:dist_version=" for a in build_args()[:-1]]
    query='mnemonic("^(SandboxExecDist|ReleaseSHA512|EmptyReleaseDist)$", deps('+TARGET+'))'
    p=subprocess.run(startup()+["aquery",*flags(),*opts,"--remote_executor=","--remote_cache=","--bes_backend=","--output=jsonproto","--noinclude_artifacts","--noinclude_commandline",query],capture_output=True)
    doc["head_graph_exit"]=p.returncode
    if p.returncode:doc["diagnostic"]=headlines(p.stderr);save("boundary",doc);return False
    from collections import Counter
    counts=Counter(a["mnemonic"] for a in json.loads(p.stdout)["actions"])
    doc["head_actions"]=dict(counts);save("boundary",doc)
    assert counts=={"EmptyReleaseDist":1,"ReleaseSHA512":4}
    build=startup()+["build","--remote_executor=","--remote_cache=","--bes_backend=","--jobs=3","--lockfile_mode=off","--strategy=GoStdlib=processwrapper-sandbox","--strategy=GoStdlibList=processwrapper-sandbox","//test/rbe/stamped_release:boundary-inputs"]
    source=Path("test/rbe/stamped_release/BUILD");original=source.read_bytes();out=Path("bazel-bin/test/rbe/stamped_release/boundary-inputs")
    try:
        for phase in ("populated","empty"):
            if phase=="empty":
                old=b'    python_dist = ":boundary-tree",\n'
                assert original.count(old)==1;source.write_bytes(original.replace(old,b""))
            p=subprocess.run(build,capture_output=True)
            doc[phase+"_exit"]=p.returncode
            if p.returncode:doc["diagnostic"]=headlines(p.stderr);save("boundary",doc);return False
            py=out/"python";items=sorted(f.name for f in py.iterdir())
            doc[phase]={"path":str(py.resolve()),"members":items};save("boundary",doc)
            if phase=="populated":
                assert len(items)==4 and list(py.glob("*.whl")) and list(py.glob("*.tar.gz"))
                for arch in ("x86_64","aarch64"):
                    f,=(out/arch).glob("*.sha512")
                    subprocess.run(["sha512sum","--check","--strict","--",f.name],cwd=f.parent,check=True,capture_output=True)
                assert b"-archive, space\n" in (out/"inputs").read_bytes()
            else:
                assert items==[] and doc["populated"]["path"]==str(py.resolve())
                no=D/"deny-gate";no.mkdir();g=no/"gcloud"
                g.write_text("#!/bin/sh\ntouch \"$FORBIDDEN_GATE\"; exit 125\n");g.chmod(0o755)
                call=subprocess.run(["tools/make_python_release.sh","publish-manifest",py],env={**os.environ,"PATH":str(no)+":/usr/bin:/bin","FORBIDDEN_GATE":str(no/"called")},capture_output=True)
                assert call.returncode==0 and b"No wheel staged" in call.stdout and not (no/"called").exists()
                doc["stale_gate_skipped"]=True
    finally:source.write_bytes(original)
    doc["verified"]=True;save("boundary",doc);return True


def bep():
    result={"complete":False,"files":[],"failures":[],"successful_actions":[]}
    expected={a["path"]:(a["owner"],a["config"],a["mnemonic"]) for a in read("graph")["actions"]}
    named={}; roots=[]
    with (D/"build.bep").open() as f:
        for line in f:
            e=json.loads(line);id_=e.get("id",{})
            if "started" in e: result["iid"]=e["started"]["uuid"]
            if "finished" in e: result["exit"]=e["finished"]["exitCode"].get("code",0)
            if "namedSetOfFiles" in e: named[id_["namedSet"]["id"]]=e["namedSetOfFiles"]
            if id_.get("targetCompleted",{}).get("label")==TARGET and not id_["targetCompleted"].get("aspect"):
                result["complete"]=bool(e["completed"].get("success"))
                for group in e["completed"].get("outputGroup",[]):
                    if group["name"]=="default": roots=[s["id"] for s in group.get("fileSets",[])]
            a=e.get("action")
            aid=id_.get("actionCompleted",{});primary=aid.get("primaryOutput")
            if a and a.get("success") and primary in expected:
                actual=(aid.get("label","").lstrip("@"),aid.get("configuration",{}).get("id"),a.get("type"))
                if actual==expected[primary]:result["successful_actions"].append(primary)
            if a and not a.get("success") and len(result["failures"])<8:
                failure={"owner":id_.get("actionCompleted",{}).get("label"),"mnemonic":a.get("type"),"exit":a.get("exitCode")}
                for stream in ("stderr","stdout"):
                    match=re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/(\d+)$",a.get(stream,{}).get("uri",""))
                    if match: failure[stream]=[match[1],int(match[2])]
                result["failures"].append(failure)
    seen=set()
    while roots:
        k=roots.pop()
        if k in seen: continue
        seen.add(k);node=named[k]
        result["files"].extend("/".join([*f.get("pathPrefix",[]),f["name"]]) for f in node.get("files",[]))
        roots.extend(v["id"] for v in node.get("fileSets",[]))
    return result


def remote_outputs(plan_):
    paths=[a["path"] for a in plan_]
    query=r'''if .file and ($paths|index($item.file.path))!=null then ["file",.id,.file]
      elif .directory and ($paths|index($item.directory.path))!=null then ["dir",.id,.directory]
      elif .spawn and ($wanted[(.spawn.targetLabel//"" | sub("^@@";""))]==.spawn.mnemonic) then
      .spawn|["spawn",(.targetLabel|sub("^@@";"")),.mnemonic,.runner,(.cacheHit//false),(.exitCode//0),
       ((.platform.properties//[])|map({key:.name,value:.value})|from_entries|[.Arch,."container-image",."workload-isolation-type"]),
       [.outputs[]? | .outputId],(if (.mnemonic=="SandboxExecDist" or .mnemonic=="ReleaseSHA512") then .args else [] end)] else empty end'''
    cmd=["jq","-c","--argjson","paths",json.dumps(paths),"--argjson","wanted",json.dumps({**{k:v[0] for k,v in KINDS.items()},TARGET:"ReleaseSHA512"}),". as $item | "+query]
    with subprocess.Popen([D/"bb","print","--raw","--compact_execution_log="+str(D/"build.compact")],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as dec:
        out=subprocess.check_output(cmd,stdin=dec.stdout,text=True)
        dec.stdout.close();assert dec.wait()==0
    material={};spawns=[]
    for line in out.splitlines():
        r=json.loads(line)
        if r[0]=="spawn": spawns.append(r)
        else: material[str(r[1])]=(r[0],r[2])
    found=[]
    for _,owner,mnem,runner,cached,code,(arch,image,isolation),ids,argv in spawns:
        matches=[(r, material[str(i)]) for r in plan_ if r["owner"]==owner and r["target"]==arch for i in ids
                 if str(i) in material and material[str(i)][1]["path"]==r["path"]]
        if not matches: continue
        (expected,joined),=matches
        kind,item=joined;root=Path(item["path"])
        children=item.get("files",[]) if kind=="dir" else [item]
        checks={}
        for child in children:
            original=root/child["path"] if kind=="dir" else root
            # The intermediate Flit tree isn't a default file. Join its original
            # compact-log child digests to the declared on-client copies.
            target=STAGE/"python"/child["path"] if owner==PY else original
            d=child["digest"];checks[str(original)]={"sha256":d["hash"],"bytes":int(d.get("sizeBytes",0))}
            sha=hashlib.sha256();sha512=hashlib.sha512()
            with target.open("rb") as file:
                for chunk in iter(lambda:file.read(1<<20),b""):
                    sha.update(chunk);sha512.update(chunk)
            assert sha.hexdigest()==d["hash"] and target.stat().st_size==int(d["sizeBytes"])
            checks[str(original)]["sha512"]=sha512.hexdigest()
        assert runner=="remote" and not cached and code==0 and image==IMAGE and isolation=="oci"
        if owner==TARGET: assert argv==expected["args"]
        assert len(checks)==(2 if owner==PY else 4 if owner==TARGET and argv[-3]=="python" else 1)
        found.append({"owner":owner,"mnemonic":mnem,"arch":arch,"config":expected["config"],"root":str(root),
                      "remote":True,"cached":cached,"image":image,"files":checks,"argv":argv})
    assert sorted(r["root"] for r in found)==sorted(r["path"] for r in plan_)
    return found


def hashfile(p):
    with p.open("rb") as f:
        h=hashlib.sha256()
        for chunk in iter(lambda:f.read(1<<20),b""): h.update(chunk)
    return h.hexdigest()


def collect():
    assert not (D/"client.rc").exists()
    g=read("graph");b=read("build")
    doc={"source":SOURCE,"invocation":b["invocation"],"verified":False}
    save("outputs",doc);doc["bep"]=bep();save("outputs",doc)
    if not doc["bep"]["complete"] or b["exit"]: return False
    assert doc["bep"]["iid"]==b["invocation"] and doc["bep"]["exit"]==0 and b["argv"]==["build",*build_args()]
    doc["fresh"]=remote_outputs(g["actions"]);save("outputs",doc)
    doc["bep_action_join"]=sorted(doc["bep"]["successful_actions"])==sorted(a["path"] for a in g["actions"])
    raw=MANIFEST.read_bytes()
    assert raw.endswith(b"\0")
    listed=[os.fsdecode(p) for p in raw[:-1].split(b"\0")]
    doc["layout"]={"files":listed,"sha256":hashlib.sha256(raw).hexdigest(),"bin":os.readlink("bazel-bin"),"out":os.readlink("bazel-out")}
    produced={};pysource=None
    for row in doc["fresh"]:
        if row["owner"]==TARGET: continue
        for name,facts in row["files"].items():
            arch="python" if row["owner"]==PY else {"amd64":"x86_64","arm64":"aarch64"}[row["arch"]]
            key=arch+"/"+Path(name).name
            produced[key]=(name,facts)
        if row["owner"]==PY:pysource=STAGE/"python"
    assert len(produced)==8 and len(listed)==6 and set(listed)=={n for n in produced if not n.startswith("python/")}
    expected={Path("inputs")}|{Path(n) for n in produced}|{Path(n+".sha512") for n in produced if not n.endswith(".deb")}
    stagefiles={p.relative_to(STAGE) for p in STAGE.rglob("*") if p.is_file()}
    assert stagefiles==expected
    stage={}
    for name,(src,facts) in produced.items():
        path=STAGE/name; same=Path(src).exists() and path.samefile(src)
        assert same or hashfile(path)==facts["sha256"]
        if not name.endswith(".deb"):
            sidecar=Path(str(path)+".sha512")
            assert sidecar.read_bytes()==(facts["sha512"]+"  "+path.name+"\n").encode()
        stage[name]={"sha256":facts["sha256"],"bytes":facts["bytes"],"samefile":same,"is_symlink":path.is_symlink()}
    doc.update(stage=stage,stage_files=sorted(str(p) for p in stagefiles),materialized=True);save("outputs",doc)
    # BEP may name a tree or enumerate its children. Every regular default
    # output and every root/complete set of children must be represented.
    prefix="bazel-out/"+STAGE.parent.resolve().relative_to(Path("bazel-out").resolve()).as_posix()+"/"+STAGE.name+"/"
    files={prefix+"inputs"}|{prefix+p for p in listed}|{prefix+p+".sha512" for p in listed if not p.endswith(".deb")}
    dirs={}
    for r in doc["fresh"]:
        if r["owner"]==TARGET:
            if r["argv"][-3]=="python": dirs[r["root"]]=set(r["files"])
            else: files.add(r["root"])
        elif r["owner"]==PY:continue
        else:files.add(r["root"])
    advertised=set(doc["bep"]["files"])
    assert files<=advertised
    rest=advertised-files
    for root,children in dirs.items():
        seen=rest&({root}|children)
        assert seen in ({root},children,children|{root})
        rest-=seen
    assert not rest
    doc.update(stage=stage,stage_files=sorted(str(p) for p in stagefiles),provider_complete=True);save("outputs",doc)
    wheel,=pysource.glob("*.whl");sdist,=pysource.glob("*.tar.gz")
    with zipfile.ZipFile(wheel) as z:
        meta,=[n for n in z.namelist() if n.endswith(".dist-info/METADATA")]
        assert "\nVersion: "+VERSION+"\n" in "\n"+z.read(meta).decode()
        for module in ("gvisor/__init__.py","gvisor/sandbox.py"): assert z.read(module)==(Path("sandboxexec/sandbox/python")/module).read_bytes()
    with tarfile.open(sdist) as tar:
        top="gvisor-"+VERSION+"/"
        assert "\nVersion: "+VERSION+"\n" in "\n"+tar.extractfile(top+"PKG-INFO").read().decode()
        for module in ("gvisor/__init__.py","gvisor/sandbox.py"): assert tar.extractfile(top+module).read()==(Path("sandboxexec/sandbox/python")/module).read_bytes()
    doc.update(python_version=VERSION,python_files=[str(wheel),str(sdist)],native={}); save("outputs",doc)
    for cpu in ("amd64","arm64"):
        native={}
        for row in doc["fresh"]:
            if row["arch"]!=cpu or row["owner"] not in KINDS or row["owner"]==PY:continue
            path=Path(row["root"]);facts={};native[path.name]=facts
            assert archive(path,cpu,facts)
        assert len(native)==3 and native["runsc.deb"]["files"]==native["gvisor.tar.bz2"]["files"]==native["gvisor.tar.zstd"]["files"]
        doc["native"][cpu]=native;save("outputs",doc)
    doc["verified"]=doc["bep_action_join"];save("outputs",doc);return doc["verified"]


if __name__=="__main__":
    try:
        result={"graph":plan,"boundary":boundary,"build":build,"collect":collect}[sys.argv[1]]()
        sys.exit(0 if result else 1)
    except (AssertionError,ValueError,TypeError,KeyError,IndexError,OSError,subprocess.SubprocessError) as ex:
        loc=traceback.extract_tb(ex.__traceback__)[-1]
        save("error-"+sys.argv[1],{"type":type(ex).__name__,"line":loc.lineno,"statement":loc.line[:240],"source":SOURCE})
        raise
