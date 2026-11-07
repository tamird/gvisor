"""Qualify the standalone shipping change with stock master Make and Bazel."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile

from archive_alias import observe
from cold_repository import headlines

BASE = "9ee2906f518e1d0f9fd109e9e1feb2812096418b"
PRODUCT = "657d01de1cf19183ea91976a7e19c7636cce0331"
WORK = Path(os.environ["RUNNER_TEMP"])/"stock-shipping"
RC = "--nosystem_rc --nohome_rc --bazelrc=" + str(WORK/"bounds.bazelrc")
KINDS = ("bz2","zstd")
PUBLIC = "//debian:gvisor-release-tar-"
NAMES = {"runsc","containerd-shim-runsc-v1",*("gvisor-bin/"+s for s in
         ("checkpointgofer","gvisor-proc-umounter","gvisor-sentry-prewarmer","gvisor_sentry",
          "runsc-fd-parking","runsc-metric-server","runsc-netgofer"))}

WRAPPER = '''#!/usr/bin/python3
import json, os, sys, uuid
from pathlib import Path
w=Path(__file__).resolve().parent.parent
args=sys.argv[1:]
sub=next((a for a in args if a in {"build","test","run","mod","query","cquery","aquery","info","shutdown"}),None)
labels=[a for a in args if a.startswith(("//debian:gvisor-release-tar-","//tools/licensecheck","//:release"))]
modes=[args[i+1] for i,a in enumerate(args[:-1]) if a=="-c"]+[a.partition("=")[2] for a in args if a.startswith("--compilation_mode=")]
row={"subcommand":sub,"labels":labels,"modes":modes}
if sub=="mod":
    row["operation"]=next((x for x in args if x in {"graph","show_repo","dump_repo_mapping"}),None)
if sub=="build" and len(labels)==1 and labels[0] in {"//debian:gvisor-release-tar-bz2","//debian:gvisor-release-tar-zstd"}:
    kind=labels[0].rsplit("-",1)[-1]
    invocation=str(uuid.uuid4())
    row["invocation"]=invocation
    i=args.index("--") if "--" in args else len(args)
    args[i:i]=["--invocation_id="+invocation,"--build_event_publish_all_actions","--build_event_json_file="+str(w/"raw"/(kind+".bep"))]
with (w/"raw"/"bazel-calls.jsonl").open("a") as f:
    f.write(json.dumps(row)+"\\n")
real=str(w/"bazel.real")
os.execv(real,[real,*args])
'''


def save(name, doc):
    content=json.dumps(doc,indent=2)+"\n"
    assert len(content)<120_000 and not re.search(r"(?i)x-buildbuddy-api-key|authorization:|github_pat_|gh[pousr]_",content)
    (WORK/"public"/(name+".json")).write_text(content)


def digest(path):
    with path.open("rb") as f:
        return hashstream(f)


def verify_source(expected):
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==expected
    subprocess.run(["git","diff","--exit-code","--quiet","HEAD","--"],check=True)


def original(name, cmd):
    log=WORK/"raw"/(name+".log")
    with log.open("wb") as f:
        rc=subprocess.run(cmd,stdout=f,stderr=f).returncode
    d={"source":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),"exit":rc,
       "log_sha256":digest(log),"log_bytes":log.stat().st_size}
    if rc:
        with log.open("rb") as f:
            first=f.read(100_000)
            f.seek(max(0,log.stat().st_size-200_000))
            d["diagnostic"]=headlines(first+b"\n"+f.read())
    save(name,d)
    print(name,"exit",rc,flush=True)
    return rc


def setup():
    verify_source(PRODUCT)
    (WORK/"bin").mkdir()
    (WORK/"downloads").mkdir()
    p = WORK/"bin/bazel"
    p.write_text(WRAPPER)
    p.chmod(0o700)
    (WORK/"bounds.bazelrc").write_text("build --jobs=4\nbuild --remote_executor=\nbuild --remote_cache=\nbuild --bes_backend=\nbuild --noshow_progress\n")
    save("source",{"master":BASE,"product":PRODUCT,"patch_id":"f7f8c0a3b9c2fb8e1b07aa706711cb23b410a2ee",
                   "bazel_sha256":digest(WORK/"bazel.real"),"host_cpu":platform.machine(),
                   "gcc":subprocess.check_output(["gcc","-dumpfullversion"],text=True).strip(),
                   "native_gcc_target":subprocess.check_output(["gcc","-dumpmachine"],text=True).strip(),
                   "cross_gcc_target":subprocess.check_output(["aarch64-linux-gnu-gcc","-dumpmachine"],text=True).strip(),
                   "gnu_tar":subprocess.check_output(["tar","--version"],text=True).splitlines()[0],
                   "free_bytes":shutil.disk_usage(WORK).free,
                   "rules_go":re.search(r'bazel_dep\(name = "rules_go", version = "([^"]+)"',Path("MODULE.bazel").read_text())[1]})


def make(name, *args):
    return original(name,["make","--no-print-directory","DOCKER_BUILD=false","STARTUP_OPTIONS="+RC,*args])


def bazel(*args):
    return ["bazel",*RC.split(),*args]


def calls():
    return [json.loads(l) for l in (WORK/"raw/bazel-calls.jsonl").read_text().splitlines()]


def check_license(base=False):
    src=BASE if base else PRODUCT
    verify_source(src)
    name="baseline-license" if base else "license"
    before=len(calls()) if (WORK/"raw/bazel-calls.jsonl").exists() else 0
    rc=make(name,"license-check")
    rows=calls()[before:]
    mods=[r["operation"] for r in rows if r["subcommand"]=="mod"]
    d=json.loads((WORK/"public"/(name+".json")).read_text())
    output=(WORK/"raw"/(name+".log")).read_text(errors="replace")
    count=re.search(r"all (\d+) dependencies have up-to-date license entries",output)
    d.update(dependencies_verified=int(count[1]) if count else None,bazel_target_seen=any("//tools/licensecheck/main:licensecheck" in r["labels"] for r in rows),
             canonical_mod_operations=mods)
    save(name,d)
    return rc==0 and bool(d["dependencies_verified"]) and d["bazel_target_seen"] and "graph" in mods and "show_repo" in mods


def graph(arch):
    cmd=bazel("aquery","-c","opt","--color=no","--curses=no","--output=jsonproto","--include_artifacts=false")
    if arch=="arm64":
        cmd.append("--config=aarch64")
    cmd.append('mnemonic("^(Tar|PackageTar)$", deps(set('+ ' '.join(PUBLIC+k for k in KINDS) + ')))')
    p=subprocess.run(cmd,capture_output=True)
    doc={"source":PRODUCT,"target":arch,"exit":p.returncode,"matched":False}
    if p.returncode:
        doc["diagnostic"]=headlines(p.stderr)
    else:
        g=json.loads(p.stdout)
        labels={str(t["id"]):t["label"].lstrip("@") for t in g["targets"]}
        cfg={str(c["id"]):c for c in g["configuration"]}
        rows={labels[str(a["targetId"])]:{"mnemonic":a["mnemonic"],"executable":a["arguments"][0],"argv":a["arguments"],
              "target_config":cfg[str(a["configurationId"])]["mnemonic"],"action_key":a["actionKey"]} for a in g["actions"]}
        doc["writers"]=rows
        cpus=("arm64","aarch64") if arch=="arm64" else ("k8","x86_64","amd64")
        doc["matched"]=rows.keys()=={PUBLIC+k+"_archive" for k in KINDS} and all(
            r["mnemonic"]=="Tar" and r["target_config"].split("-")[0] in cpus and "opt" in r["target_config"] for r in rows.values())
    save("graph-"+arch,doc)
    return doc["matched"]


def hashstream(f,head=b""):
    h=hashlib.sha256(head)
    while chunk:=f.read(1024*1024):
        h.update(chunk)
    return h.hexdigest()


def members(tar):
    records={}
    for f in tar:
        assert f.name in NAMES|{"gvisor-bin"} and f.name not in records
        item={"type":f.type.decode(),"size":f.size,"uid":f.uid,"gid":f.gid,"uname":f.uname,"gname":f.gname,"mode":oct(f.mode),"mtime":f.mtime,"link":f.linkname}
        records[f.name]=item
        if f.isfile():
            assert 0<f.size<300_000_000
            with tar.extractfile(f) as src:
                head=src.read(20)
                assert head[:7]==b"\x7fELF\x02\x01\x01"
                item.update(machine=struct.unpack_from("<H",head,18)[0],sha256=hashstream(src,head))
    return records


def bep(kind):
    iid=None
    finish=None
    allsets={}
    target_sets=[]
    actions=[]
    where=WORK/"raw"/(kind+".bep")
    if not where.is_file():
        return {}
    with where.open() as stream:
        for line in stream:
            e=json.loads(line)
            if "started" in e:
                iid=e["started"].get("uuid")
            if "finished" in e:
                finish=int(e["finished"].get("exitCode",{}).get("code",0))
            if "namedSetOfFiles" in e:
                allsets[e["id"]["namedSet"]["id"]]=e["namedSetOfFiles"]
            if e["id"].get("targetCompleted",{}).get("label")==PUBLIC+kind:
                target_sets.extend(s["id"] for g in e.get("completed",{}).get("outputGroup",[])
                                   if g["name"]=="default" for s in g.get("fileSets",[]))
            a=e["id"].get("actionCompleted",{})
            if a.get("label")==PUBLIC+kind+"_archive" and e.get("action",{}).get("type")=="Tar":
                actions.append({"success":e["action"].get("success",False),"output":a.get("primaryOutput")})
    names=[]
    seen=set()
    while target_sets:
        key=target_sets.pop()
        if key in seen:
            continue
        seen.add(key)
        node=allsets[key]
        names.extend("/".join([*f.get("pathPrefix",[]),f["name"]]) for f in node.get("files",[]))
        target_sets.extend(s["id"] for s in node.get("fileSets",[]))
    return {"invocation":iid,"exit":finish,"default_files":sorted(names),"tar_actions":actions}


def release():
    verify_source(PRODUCT)
    before=len(calls()) if (WORK/"raw/bazel-calls.jsonl").exists() else 0
    rc=make("make-release","DESTINATION="+str(WORK/"downloads"),"release-tarball")
    selected=[r for r in calls()[before:] if r.get("invocation")]
    stage=original("installed-layout",bazel("build","-c","opt","//:release",*(PUBLIC+k+".tar."+k for k in KINDS))) if not rc else None
    report={"source":PRODUCT,"make_exit":rc,"staging_exit":stage,"make_bazel_builds":selected,"formats":{},"verified":False}
    save("shipping",report)
    stage_dir=Path("bazel-bin/release")
    for kind in KINDS:
        name="gvisor.tar."+kind
        packed=WORK/"downloads"/name
        row={"bep":bep(kind),"copied":packed.is_file()}
        report["formats"][kind]=row
        save("shipping",report)
        if not packed.is_file():
            continue
        if kind=="bz2":
            with tarfile.open(packed,"r|bz2") as tf:
                rows=members(tf)
            tar_args=["tar","-xjOf",packed,"runsc"]
        else:
            with subprocess.Popen(["zstd","-dcq","--",packed],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as dec:
                with tarfile.open(fileobj=dec.stdout,mode="r|") as tf:
                    rows=members(tf)
                while dec.stdout.read(1024*1024):
                    pass
                assert dec.wait()==0
            tar_args=["tar","--zstd","-xOf",packed,"runsc"]
        with subprocess.Popen(tar_args,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as tar:
            runsc=hashstream(tar.stdout)
            row["publisher_exit"]=tar.wait()
        canonical=Path("bazel-bin/debian")/name
        archive_sha=digest(packed)
        link=canonical.parent/("gvisor-release-tar-"+kind+".tar."+kind)
        row.update(sha256=archive_sha,bytes=packed.stat().st_size,headers=rows,publisher_runsc=runsc,
                   canonical_matches=digest(canonical)==archive_sha,
                   alias=observe(link,canonical,Path.home(),Path.home()))
        row["matches"]=bool(set(rows)==NAMES|{"gvisor-bin"} and row["canonical_matches"]
                            and row["publisher_exit"]==0 and runsc==rows["runsc"]["sha256"]
                            and row["alias"].get("mapped_samefile") and row["alias"].get("mapped_sha256")==archive_sha
                            and any(r["labels"]==[PUBLIC+kind] and r["invocation"]==row["bep"].get("invocation") and "opt" in r["modes"] for r in selected)
                            and any(n.endswith("/"+name) or n==name for n in row["bep"].get("default_files",[]))
                            and len(row["bep"].get("tar_actions",[]))==1 and row["bep"]["tar_actions"][0]["success"]
                            and row["bep"].get("exit")==0 and all(x["uid"]==x["gid"]==0 and x["mtime"]==946684800
                                    and x["mode"]=="0o755" and x["uname"]==x["gname"]=="" for x in rows.values())
                            and all(rows[n].get("machine")==62 and (stage_dir/n).is_file() and digest(stage_dir/n)==rows[n]["sha256"] for n in NAMES))
        save("shipping",report)
    f=report["formats"]
    report["verified"]=bool(rc==stage==0 and all(f[k].get("matches") for k in KINDS) and f["bz2"]["headers"]==f["zstd"]["headers"])
    save("shipping",report)
    return report["verified"]


if __name__=="__main__":
    op=sys.argv[1]
    if op=="setup":
        setup()
    elif op=="license":
        sys.exit(not check_license(len(sys.argv)>2))
    elif op=="test":
        sys.exit(original("license-test",bazel("test","//tools/licensecheck:licensecheck_test")))
    elif op=="graphs":
        sys.exit(not all([graph(arch) for arch in ("amd64","arm64")]))
    elif op=="shipping":
        sys.exit(not release())
    else:
        raise ValueError(op)
