"""Compare the native package action and its product across worker images."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
import zipfile

import check as q

SOURCE = "700b0bbea70ff699553d8e40827a2f3dab4afdd2"
REPOS = {"original":"01446201fd8e758267f4e1abd9727926655110b7","candidate":"1702e63c4673bf8c00df82a1613c2359895fbe20"}
OUT = {"source":SOURCE,"suppliers":REPOS,"phases":{},"expected":["original-normal","original-scratch","candidate-scratch"],"verdict":"INCOMPLETE"}
TARGET = "//test/rbe/zip_writer_probe:foreign_archive"
EMPTY = "@rules_pkg//tests/zip:test_zip_empty"
PKG = Path("test/rbe/zip_writer_probe")
DATA = b"Compressed from the same bytes.\n" * 200
SENTINEL = "rules_pkg_inherited_value"

def save():
    (q.ROOT/"rules-pkg-zip.json").write_text(json.dumps(OUT,indent=2,sort_keys=True)+"\n")

def require(ok, text):
    if not ok:raise ValueError(text)

def safe_lines(s):
    return [l[:320] for l in s.splitlines() if any(w in l.lower() for w in ("error:", "executable","no such file","not visible", "no repository", "could not")) and not any(w in l.lower() for w in ("key=", "header", "secret", "authorization", "token", "command line"))][-11:]

def verify_product(path, fixture):
    b=path.read_bytes()
    record={"size":len(b),"digest":q.digest(b)}
    with zipfile.ZipFile(path) as z:
        if fixture:
            infos=[i for i in z.infolist() if not i.is_dir()]
            require(len(infos)==1,"expected one regular ZIP member")
            i=infos[0]
            record["member"]={"path":i.filename,"mode":(i.external_attr>>16)&0o7777,"time":i.date_time,"compression":i.compress_type,"crc":i.CRC}
            require(i.filename.startswith("demo/") and i.filename.endswith("/payload.txt") and z.read(i)==DATA, "zip member or payload mismatch")
            require(record["member"]["mode"]==0o640 and i.compress_type==zipfile.ZIP_DEFLATED and tuple(i.date_time)==(2001,9,9,1,46,40), "ZIP metadata differs from request")
        else:
            require(not z.infolist(),"empty zip contains a member")
    return record

def run(repo,image):
    name=repo+("-normal" if image==q.GNU_IMAGE else "-scratch")
    phase=OUT["phases"][name]={"image":image,"supplier":REPOS[repo],"invocation":str(uuid.uuid4()),"start":q.utc(),"actions":[],"errors":[],"verified":False}
    save()
    checkout=(Path.cwd().parent/repo).resolve()
    argv=["bazel","--bazelrc="+os.environ["QA_RC"],"build","--config=x86_64","--config=static-python","--jobs=32",
          "--override_module=rules_pkg="+str(checkout),"--extra_execution_platforms=//tools/bazeldefs:linux_amd64",
          "--remote_default_exec_properties=Arch=amd64","--remote_default_exec_properties=OSFamily=linux",
          "--remote_default_exec_properties=workload-isolation-type=oci","--remote_default_exec_properties=container-image="+image,
          "--action_env=RULES_PKG_ZIP_PROBE="+SENTINEL,"--remote_download_outputs=all",
          "--modify_execution_info=^PackageZip$=+no-remote-cache","--keep_going",
          "--invocation_id="+phase["invocation"],"--build_event_json_file="+str(q.ROOT/(name+".bep")),
          "--execution_log_json_file="+str(q.ROOT/(name+".exec")),TARGET,EMPTY]
    p=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    phase.update(exit=p.returncode,log_sha256=q.digest(p.stdout.encode()),diagnostics=safe_lines(p.stdout))
    save()
    events=list(q.rows(q.ROOT/(name+".bep")))
    spawns=[r for r in q.rows(q.ROOT/(name+".exec")) if r.get("mnemonic")=="PackageZip"]
    failed=repo=="original" and image==q.IMAGE
    try:
        require((p.returncode!=0)==failed,f"unexpected Bazel exit {p.returncode}")
        require(any(e.get("started",{}).get("uuid")==phase["invocation"] for e in events),"missing invocation identity")
        require(any("finished" in e for e in events),"missing final Build Event")
        if failed:
            # Remote startup errors may happen before the compact spawn log is written.
            require(any("PackageZip" in l for l in p.stdout.splitlines()),"failure did not identify PackageZip")
            require(any(x in p.stdout.lower() for x in ("exec format","no such file", "file not found", "failed_precondition", "failed to start")),"failure did not identify process startup")
        else:
            require(len(spawns)==2,"two exact ZIP actions expected")
        execroot=Path(subprocess.check_output(["bazel","--bazelrc="+os.environ["QA_RC"],"info","execution_root"],text=True).strip())
        for spawn in spawns:
            args=spawn["commandArgs"]
            props={v["name"]:v["value"] for v in spawn["platform"]["properties"]}
            env={v["name"]:v.get("value","") for v in spawn.get("environmentVariables",[])}
            row={"label":spawn.get("targetLabel"),"runner":spawn.get("runner"),"cached":spawn.get("cacheHit",False),"argv0":args[0],"exit":spawn.get("exitCode",0),"image":props.get("container-image"),"environment":{k:env.get(k) for k in ("LANG","LC_CTYPE","PYTHONIOENCODING","PYTHONUTF8","RULES_PKG_ZIP_PROBE","RUNFILES_DIR")}}
            phase["actions"].append(row)
            require(props.get("Arch")=="amd64" and props.get("container-image")==image and not row["cached"],"wrong worker or cache")
            require(row["environment"]["RULES_PKG_ZIP_PROBE"]==SENTINEL and [env.get(k) for k in ("LANG","LC_CTYPE","PYTHONIOENCODING","PYTHONUTF8")]==["en_US.UTF-8","UTF-8","UTF-8","1"],"lost incoming ZIP environment")
            if not failed:
                require(row["runner"]=="remote" and row["exit"]==0,"ZIP action did not execute remotely")
                if repo=="original":
                    b=(execroot/args[0]).read_bytes()
                    row["initial_line"]=b.splitlines()[0].decode()
                    require(row["initial_line"]=="#!/usr/bin/env bash","stock writer is not script bootstrap")
                else:
                    row["interpreter"]=next(d["digest"]["hash"] for d in spawn["inputs"] if d["path"]==args[0])
                    require(row["interpreter"]==q.PYTHON and any("stage2" in a for a in args) and env.get("RUNFILES_DIR","").endswith(".runfiles"),"candidate bypassed configured stage two or interpreter")
                paths=[o["path"] for o in spawn.get("actualOutputs",[]) if o["path"].endswith(".zip")]
                require(len(paths)==1,"wrong ZIP output set")
                row["archive"]=verify_product(execroot/paths[0],"foreign_archive" in paths[0])
        phase["verified"]=True
    except (OSError, ValueError, StopIteration, KeyError, subprocess.SubprocessError) as exc:
        phase["errors"].append(str(exc))
    phase["finish"]=q.utc()
    save()
    print(name,phase["invocation"],phase["exit"],phase["errors"],phase["diagnostics"] if phase["errors"] else "",flush=True)

require(q.git("rev-parse","HEAD").decode().strip()==SOURCE,"wrong gVisor fixture source")
for folder, sha in REPOS.items():
    require(subprocess.check_output(["git","-C",str(Path.cwd().parent/folder),"rev-parse","HEAD"],text=True).strip()==sha,"wrong supplier")
PKG.mkdir(parents=True)
(PKG/"payload.txt").write_bytes(DATA)
(PKG/"BUILD").write_text('load("@rules_pkg//pkg:zip.bzl", "pkg_zip")\npkg_zip(name="foreign_archive", srcs=["payload.txt"], mode="0640", timestamp=1000000000, package_dir="/demo")\n')
OUT["fixture_sha256"]=q.digest((PKG/"BUILD").read_bytes())
save()
for repo,image in [("original",q.GNU_IMAGE),("original",q.IMAGE),("candidate",q.IMAGE)]:
    try:run(repo,image)
    except (OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:OUT.setdefault("errors",[]).append(str(exc));save()
present=[{r["label"]:r["archive"] for r in OUT["phases"].get(n,{}).get("actions",[]) if "archive" in r} for n in ("original-normal","candidate-scratch")]
OUT["bytes_identical"]=len(present[0])==2 and present[0]==present[1]
OUT["verdict"]="PASS" if OUT["bytes_identical"] and not OUT.get("errors") and all(OUT["phases"].get(n,{}).get("verified") for n in OUT["expected"]) else "FAIL"
OUT["finished"]=q.utc()
save()
raise SystemExit(OUT["verdict"]!="PASS")
