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
SOURCE="331c9f51e7e380fc243f8dcae646a9794f2aef65"
STAMP="release-20261106.0"
VERSION="2026.10.26.13"
TARGET="//debian:release-inputs"
PY="//sandboxexec/sandbox/python:dist"
IMAGE="docker://cgr.dev/chainguard/static@sha256:fc42af33e643f20d08baf03cddcc1c02ba57052dfb02edb0d79f3fc47bcea8d9"
WORKER="//test/rbe/stamped_release:"
KINDS={"//debian:debian":("MakeDeb","runsc.deb"),"//debian:gvisor-release-tar-bz2_archive":("Tar","gvisor.tar.bz2"),
       "//debian:gvisor-release-tar-zstd_archive":("Tar","gvisor.tar.zstd"),PY:("SandboxExecDist","dist")}
MANIFEST=Path("bazel-bin/debian/release-inputs.inputs0")
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
    assert len(chunks)==2 and chunks[0].count("bazel build")==1 and chunks[0].endswith(TARGET)
    assert chunks[1].startswith("mapfile -d '' -t inputs < bazel-bin/debian/release-inputs.inputs0") and chunks[1].endswith('tools/make_release.sh repo.key repo "${inputs[@]}"')
    return chunks


def flags():
    return ["--config=x86_64","--lockfile_mode=off","--jobs=48","--platform_mappings=test/rbe/stamped_release/platform_mappings",
            "--extra_execution_platforms="+",".join(WORKER+a for a in ("amd64","arm64")),
            "--extra_toolchains="+",".join("//test/rbe/go_bootstrap:linux_"+a+"_ash" for a in ("amd64","arm64")),
            "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
            "--@rules_python//python/config_settings:bootstrap_impl=native",
            "--modify_execution_info=^(MakeDeb|Tar|SandboxExecDist)$=+no-remote-cache","--color=no","--curses=no","--noshow_progress"]


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
                    "--output=jsonproto","--include_artifacts","--noinclude_commandline"]
    p=subprocess.run(base+['mnemonic("^(MakeDeb|Tar|SandboxExecDist)$", deps('+TARGET+'))'],capture_output=True)
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
        if owner not in KINDS or a["mnemonic"]!=KINDS[owner][0]: continue
        conf=configs[str(a["configurationId"])]
        cpu={"k8":"amd64","x86_64":"amd64","amd64":"amd64","arm64":"arm64","aarch64":"arm64"}[conf["mnemonic"].split("-")[0]]
        paths=[]
        for output in a["outputIds"]:
            parts=[]; node=art[str(output)]
            while node in frags:
                v=frags[node];parts.append(v["label"]);node=str(v.get("parentId",""))
            path="/".join(reversed(parts))
            if path.endswith("/"+KINDS[owner][1]): paths.append(path)
        assert len(paths)==1
        doc["actions"].append({"owner":owner,"mnemonic":a["mnemonic"],"target":cpu,"worker":a["executionPlatform"].lstrip("@"),
                               "config":conf["checksum"],"path":paths[0]})
    expect={(o,c) for o in KINDS for c in (("amd64",) if o==PY else ("amd64","arm64"))}
    doc["verified"]=(len(doc["actions"])==len(expect) and {(a["owner"],a["target"]) for a in doc["actions"]}==expect and
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
    repo=Path.cwd()
    # Run this small fixture locally, separately from the one remote release build.
    target="//test/rbe/stamped_release:boundary-inputs"
    p=subprocess.run(startup()+["build","--remote_executor=","--remote_cache=","--bes_backend=","--jobs=3",target],capture_output=True)
    doc={"exit":p.returncode,"verified":False}
    if p.returncode:
        doc["diagnostic"]=headlines(p.stderr);save("boundary",doc);return False
    raw=Path("bazel-bin/test/rbe/stamped_release/boundary-inputs.inputs0").read_bytes()
    records=raw[:-1].split(b"\0")
    assert raw.endswith(b"\0") and len(set(records))==len(records)==3 and all(b" " in x and b"," in x for x in records)
    assert sum(b"\n" in x for x in records)==2
    roots=[Path(os.fsdecode(x)) for x in records]
    tree,=[p for p in roots if p.is_dir()]
    assert (tree/"-child,with\nnewline").read_bytes()==b"boundary"
    client=D/"boundary-client";(client/"bazel-bin/debian").mkdir(parents=True)
    (client/"bazel-out").symlink_to(repo/"bazel-out")
    (client/MANIFEST).write_bytes(raw)
    tools=client/"tools";tools.mkdir(); probe=tools/"make_release.sh"
    probe.write_text("#!/usr/bin/python3\nimport os,sys,pathlib\npathlib.Path('argv0').write_bytes(b'\\0'.join(map(os.fsencode,sys.argv[1:]))+b'\\0')\n")
    probe.chmod(0o755)
    subprocess.run(["bash","-euc",blocks()[1]],cwd=client,env={**os.environ,"RELEASE_NIGHTLY":"false"},check=True)
    assert (client/"argv0").read_bytes()==b"repo.key\0repo\0"+raw
    # Exercise the raw-file case (including GNU file's link policy) without a package build.
    # Check raw ELF detection before deliberately stopping this synthetic APT phase.
    rawcase=D/"raw-control";rawcase.mkdir();(rawcase/"tools").mkdir()
    (rawcase/"tools/make_apt.sh").write_text("#!/bin/sh\nexit 67\n");(rawcase/"tools/make_apt.sh").chmod(0o755)
    (rawcase/"fake.key").write_bytes(b"unused in the raw-file control")
    link=rawcase/"-native, link\n";link.symlink_to("/usr/bin/true")
    nested=rawcase/"stage";nested.mkdir();(nested/"unselected").symlink_to("/usr/bin/true")
    real=str(repo/"tools/make_release.sh");rawproc=subprocess.run([real,"fake.key","result",str(link),str(nested)],cwd=rawcase,capture_output=True,env={**os.environ,"NIGHTLY":"true"})
    result=rawcase/"result/nightly/latest/x86_64"
    assert rawproc.returncode==67 and (result/link.name).read_bytes()==Path("/usr/bin/true").read_bytes()
    assert set(os.listdir(result))=={link.name,link.name+".sha512"}
    subprocess.run(["sha512sum","--check","--strict","--",link.name+".sha512"],cwd=result,check=True,capture_output=True)
    dirlink=rawcase/"directory-link";dirlink.symlink_to(nested)
    denied=subprocess.run([real,"fake.key","rejected",str(dirlink)],cwd=rawcase,capture_output=True)
    assert denied.returncode and not (rawcase/"rejected").exists()
    doc.update(verified=True,manifest_roots=len(roots),nul_argv_sha256=hashlib.sha256(raw).hexdigest(),tree_child_newline=True,
               direct_elf_link=True,ignored_descendant_link=True,refused_directory_link=True,raw_only_control_exit=rawproc.returncode)
    save("boundary",doc);return True


def bep():
    result={"complete":False,"files":[],"failures":[]}
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
       [.outputs[]? | .outputId]] else empty end'''
    cmd=["jq","-c","--argjson","paths",json.dumps(paths),"--argjson","wanted",json.dumps({k:v[0] for k,v in KINDS.items()}),". as $item | "+query]
    with subprocess.Popen([D/"bb","print","--raw","--compact_execution_log="+str(D/"build.compact")],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as dec:
        out=subprocess.check_output(cmd,stdin=dec.stdout,text=True)
        dec.stdout.close();assert dec.wait()==0
    material={};spawns=[]
    for line in out.splitlines():
        r=json.loads(line)
        if r[0]=="spawn": spawns.append(r)
        else: material[str(r[1])]=(r[0],r[2])
    found=[]
    for _,owner,mnem,runner,cached,code,(arch,image,isolation),ids in spawns:
        expected,=[r for r in plan_ if r["target"]==arch and r["owner"]==owner]
        joined,=[material[str(i)] for i in ids if str(i) in material and material[str(i)][1]["path"]==expected["path"]]
        kind,item=joined;root=Path(item["path"])
        children=item.get("files",[]) if kind=="dir" else [item]
        checks={}
        for child in children:
            target=root/child["path"] if kind=="dir" else root
            d=child["digest"];checks[str(target)]={"sha256":d["hash"],"bytes":int(d.get("sizeBytes",0))}
            assert target.is_file() and hashfile(target)==d["hash"] and target.stat().st_size==int(d["sizeBytes"])
        assert runner=="remote" and not cached and code==0 and image==IMAGE and isolation=="oci"
        assert len(checks)==(2 if owner==PY else 1)
        found.append({"owner":owner,"mnemonic":mnem,"arch":arch,"config":expected["config"],"root":str(root),
                      "remote":True,"cached":cached,"image":image,"files":checks})
    assert sorted((r["owner"],r["arch"]) for r in found)==sorted((r["owner"],r["target"]) for r in plan_)
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
    save("outputs",doc); doc["bep"]=bep();save("outputs",doc)
    if not doc["bep"]["complete"] or b["exit"]: return False
    assert doc["bep"]["iid"]==b["invocation"] and doc["bep"]["exit"]==0 and b["argv"]==["build",*build_args()]
    raw=MANIFEST.read_bytes();assert raw.endswith(b"\0")
    names=[os.fsdecode(x) for x in raw[:-1].split(b"\0")]
    assert len(names)==len(set(names))==7 and set(names)=={a["path"] for a in g["actions"]}
    doc.update(manifest_sha256=hashlib.sha256(raw).hexdigest(),paths=names,
               client_links={p:os.readlink(p) for p in ("bazel-bin","bazel-out")})
    assert all(Path(n).is_file() or Path(n).is_dir() for n in names)
    save("outputs",doc);doc["fresh"]=remote_outputs(g["actions"]);save("outputs",doc)
    pyrow,=[r for r in doc["fresh"] if r["owner"]==PY]
    pyr=Path(pyrow["root"]);wheel,=pyr.glob("*.whl");sdist,=pyr.glob("*.tar.gz")
    with zipfile.ZipFile(wheel) as z:
        meta,=[n for n in z.namelist() if n.endswith(".dist-info/METADATA")]
        assert "\nVersion: "+VERSION+"\n" in "\n"+z.read(meta).decode()
        for module in ("gvisor/__init__.py","gvisor/sandbox.py"):
            assert z.read(module)==(Path("sandboxexec/sandbox/python")/module).read_bytes()
    with tarfile.open(sdist) as tar:
        top="gvisor-"+VERSION+"/"
        assert "\nVersion: "+VERSION+"\n" in "\n"+tar.extractfile(top+"PKG-INFO").read().decode()
        for module in ("gvisor/__init__.py","gvisor/sandbox.py"):
            assert tar.extractfile(top+module).read()==(Path("sandboxexec/sandbox/python")/module).read_bytes()
    advertised=set(doc["bep"]["files"])
    manifest_path = "bazel-out/"+MANIFEST.resolve().relative_to(Path("bazel-out").resolve()).as_posix()
    native_and_manifest={p for p in names if p!=str(pyr)} | {manifest_path}
    children={str(wheel),str(sdist)}
    assert native_and_manifest.issubset(advertised)
    assert advertised-native_and_manifest in ({str(pyr)},children,children|{str(pyr)})
    doc["manifest_exec_path"]=manifest_path
    doc.update(python_version=VERSION,python_files=[str(wheel),str(sdist)],native={});save("outputs",doc)
    for cpu in ("amd64","arm64"):
        native={}
        for r in doc["fresh"]:
            if r["arch"]!=cpu or r["owner"]==PY: continue
            path=Path(r["root"]);row={};native[path.name]=row
            assert archive(path,cpu,row)
        assert len(native)==3
        assert native["runsc.deb"]["files"]==native["gvisor.tar.bz2"]["files"]==native["gvisor.tar.zstd"]["files"]
        doc["native"][cpu]=native;save("outputs",doc)
    doc["verified"]=True;save("outputs",doc);return True


if __name__=="__main__":
    try:
        result={"graph":plan,"boundary":boundary,"build":build,"collect":collect}[sys.argv[1]]()
        sys.exit(0 if result else 1)
    except (AssertionError,ValueError,TypeError,KeyError,IndexError,OSError,subprocess.SubprocessError) as ex:
        loc=traceback.extract_tb(ex.__traceback__)[-1]
        save("error-"+sys.argv[1],{"type":type(ex).__name__,"line":loc.lineno,"statement":loc.line[:240],"source":SOURCE})
        raise
