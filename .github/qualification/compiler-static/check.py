"""Compare the six real portable Go targets with and without their transition."""
import hashlib
import json
import mmap
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import tempfile
import time

import qualification_report as report

sources = {"explicit":"8306976dbfd8b5c18c7386a9cd788d33cb99a8b1", "reverted":"54aad070c47278d39f19ec4e450431889b1a63bd"}
paths = {"shim/BUILD","test/benchmarks/defs.bzl","test/cmd/test_app/BUILD","tools/gvisor2pcap/BUILD","tools/ioctl_sniffer/BUILD","webhook/BUILD"}
labels = ["//tools/gvisor2pcap:gvisor2pcap","//shim:containerd-shim-runsc-v1","//tools/ioctl_sniffer:run_sniffer",
          "//test/cmd/test_app:test_app","//webhook:webhook","//test/benchmarks/base:startup_test"]
hook = "//tools/ioctl_sniffer:ioctl_hook_gnu"
arch = os.environ["ARCH"]
machine = {"amd64":62,"arm64":183}[arch]
out=Path(os.environ["RUNNER_TEMP"])/"compiler-static"
out.mkdir(exist_ok=True)
facts={"sources":sources,"carrier":os.environ["GITHUB_SHA"],"host":platform.machine(),"target":arch,"phases":[],"status":"INCOMPLETE"}
pcap_header = struct.pack("<IHHiiii",0xa1b2c3d4,2,4,0,0,120,101)
script=Path(__file__).resolve().parent

def save():
    (out/"result.json").write_text(json.dumps(facts,indent=2,sort_keys=True)+"\n")

def check(ok, hint):
    if not ok:
        raise ValueError(hint)

def cmd(argv):
    return subprocess.check_output(argv, text=True).strip()

def diagnostics(raw):
    text=raw.decode(errors="replace").splitlines()
    return [line[:500] for line in text if not any(k in line.lower() for k in ("api-key","remote_header","bes_header","token="))
            and ("ERROR:" in line or "error:" in line or "failed" in line.lower() or "undeclared" in line.lower() or "undefined reference" in line.lower() or "cannot find" in line.lower())][:18]

def image(path):
    with path.open("rb") as file:
        head=file.read(20)
    check(head[:6]==b"\x7fELF\x02\x01" and struct.unpack_from("<H",head,18)[0]==machine,"unexpected ELF machine")
    ph=cmd(["readelf","-lW",path])
    dy=cmd(["readelf","-dW",path])
    interps=re.findall(r"Requesting program interpreter: ([^\]]+)",ph)
    needed=re.findall(r'\(NEEDED\).*?Shared library: \[([^]]+)\]',dy)
    check(len(interps)==len([line for line in ph.splitlines() if re.match(r"\s*INTERP\s",line)])<=1,"interpreter")
    check(len(needed)==sum("(NEEDED)" in line for line in dy.splitlines()),"needed entries")
    return {"elf_machine":machine,"elf_type":struct.unpack_from("<H",head,16)[0],"interpreter":interps or None,"needed":needed}

def go_info(path):
    info=subprocess.run(["go","version","-m",path],text=True,capture_output=True)
    details=info.stdout.splitlines()
    settings={}
    for line in details[1:]:
        fields=line.strip().split("\t")
        if len(fields)==2 and fields[0]=="build" and "=" in fields[1]:
            key,value=fields[1].split("=",1)
            if key in {"CGO_ENABLED","GOARCH","GOOS","-race","-buildmode","-compiler"}:
                settings[key]=value
    symbols=set()
    # readelf can inspect binaries whose Go build settings do not identify
    # linker mode. Symbol evidence is recorded independently of their absence.
    with subprocess.Popen(["readelf","-Ws",path],stdout=subprocess.PIPE,text=True) as p:
        for line in p.stdout:
            if fields:=line.split():
                symbol=fields[-1]
                if symbol in {"_cgo_init","runtime/cgo.set_crosscall2","runtime.raceinit","__tsan_init","__tsan_go_start","runtime.raceenabled"}:
                    symbols.add(symbol)
        check(p.wait()==0,"readelf failed")
    with path.open("rb") as stream, mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ) as contents:
        function_names=[name for name in ("runtime.racefuncenter","runtime.racefuncexit","runtime.raceReadObjectPC","__tsan_go_start","runtime/cgo.set_crosscall2","net._Cfunc_getaddrinfo") if contents.find(name.encode()+b"\x00")>=0]
    return {"function_names_in_bytes":function_names,"version_m_exit":info.returncode,"go_version":(details[0].split()[-1] if details else None),
            "settings":settings,"symbols":sorted(symbols)}

def launch(path, digest):
    ordinary=subprocess.run([str(path.resolve())],input=b"",capture_output=True,timeout=20,env={"TZ":"UTC"})
    with tempfile.TemporaryDirectory(dir=out) as tn:
        d=Path(tn)
        shutil.copy2(path,d/"tool")
        result=json.loads(cmd(["sudo","-n","python3",script/"boot.py",d,digest]))
    ordinary_row={"exit":ordinary.returncode,"pcap_header":ordinary.stdout==pcap_header,
                  "stdout_sha256":hashlib.sha256(ordinary.stdout).hexdigest(),"stderr_sha256":hashlib.sha256(ordinary.stderr).hexdigest()}
    result["pcap_header"]=result.get("stdout_hex")==pcap_header.hex() and result.get("stdout_bytes")==24
    return {"ordinary":ordinary_row,"empty_chroot":result}

def physical(execution, outputs):
    # Print the same compact binary used by the canonical qualification; stream
    # the pinned decoder so unselected source contents aren't retained.
    selected={v["sha256"] for v in outputs.values()}
    files={}
    actions=[]
    partial=[]
    bb=Path(os.environ["RUNNER_TEMP"])/"bb"
    with subprocess.Popen([bb,"print","--raw=true","--sort=false","--compact_execution_log="+str(execution)],stdout=subprocess.PIPE) as decoder:
        for line in decoder.stdout:
            partial.append(line)
            if line.rstrip(b"\r\n")!=b"}":
                continue
            record=json.loads(b"".join(partial))
            partial.clear()
            if (f:=record.get("file")) and f.get("digest",{}).get("hash") in selected:
                files[record["id"]]=f
            if (s:=record.get("spawn")) and s["mnemonic"] in ("GoLink","CppLink","GoTestGenTest"):
                actions.append(s)
        check(not partial and decoder.wait()==0,"compact log decoder")
    for label,v in outputs.items():
        records=[]
        for a in actions:
            matches=[files[o["outputId"]] for o in a.get("outputs",[]) if o.get("outputId") in files and files[o["outputId"]]["digest"]["hash"]==v["sha256"]]
            if not matches:
                continue
            argv=a.get("args",[])
            # Do not infer -race from this file being under a race config.
            flags=sorted({word for arg in argv for word in ("-race","-static","-linkmode=external") if word in arg.split()})
            records.append({"owner":a["targetLabel"],"mnemonic":a["mnemonic"],"paths":[x["path"] for x in matches],
                            "runner":a.get("runner"),"cache_hit":a.get("cacheHit",False),"exit":a.get("exitCode",0),"flags":flags,
                            "properties":{p["name"]:p["value"] for p in a.get("platform",{}).get("properties",[]) if p["name"] in ("Arch","OSFamily","container-image")},
                            "action":a.get("digest"),"argv_sha256":hashlib.sha256(b"\0".join(x.encode() for x in argv)).hexdigest()})
        v["equal_byte_link_actions"]=records

def phase(variant, mode):
    source=sources[variant]
    row={"source":source,"setting":variant,"mode":mode,"started":time.time(),"labels":labels,"outputs":{}}
    facts["phases"].append(row);save()
    prefix=out/(variant+"-"+mode)
    events=prefix.with_suffix(".bep")
    execution=prefix.with_suffix(".binpb")
    args=(["--config=race","--@io_bazel_rules_go//go/config:race"] if mode=="race" else []) + ["--@io_bazel_rules_go//go/config:pure=false"]
    selected=[*labels,hook]
    expr='str(target.label) + "\\t" + (providers(target)["DefaultInfo"].files_to_run.executable.path if providers(target)["DefaultInfo"].files_to_run.executable else "<file>")'
    query=subprocess.run(["bazel","cquery","--keep_going",*args,"--output=starlark","--starlark:expr="+expr,"set("+" ".join(selected)+")"],capture_output=True)
    row["cquery_exit"]=query.returncode
    row["cquery_diagnostic"]=diagnostics(query.stderr)
    executables={}
    for line in query.stdout.decode().splitlines():
        if "\t" in line:
            label,path=line.split("\t",1)
            executables[label.removeprefix("@@").removeprefix("@")]=path
    row["configured_executables"]=executables
    run=subprocess.run(["bazel","build",*args,"--keep_going","--remote_download_outputs=toplevel",
                        "--build_event_json_file="+str(events),"--nobuild_event_json_file_path_conversion",
                        "--execution_log_compact_file="+str(execution),"--modify_execution_info=GoLink=+no-remote-cache",*selected],
                       stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    row["native_exit"]=run.returncode;row["diagnostics"]=diagnostics(run.stdout);save()
    owners={}
    missing={}
    if events.is_file():
        for line in events.open():
            event=json.loads(line)
            ident=event.get("id",{})
            if started:=event.get("started"):
                row["invocation"]=started["uuid"]
            if "finished" in event:
                row["finished"]=event["finished"].get("exitCode")
            if tr:=ident.get("targetCompleted"):
                if tr.get("aspect") or tr["label"] not in selected:
                    continue
                if event.get("completed",{}).get("success"):
                    owners[tr["label"]]=report.Owner.parse(tr)
                else:
                    missing[tr["label"]]="did not complete"
            if (a:=event.get("action")) and not a.get("success") and len(row.setdefault("failed_actions",[]))<18:
                action_id=ident.get("actionCompleted",{})
                detail=a.get("failureDetail",{})
                row["failed_actions"].append({"owner":action_id.get("label"),"output":action_id.get("primaryOutput"),"mnemonic":a.get("type"),"exit":a.get("exitCode"),
                    "category":sorted(set(detail)-{"message"}),"message":str(detail.get("message",""))[:500] if "api-key" not in str(detail).lower() else "redacted"})
            if "testResult" in event:
                raise ValueError("unexpected TestRunner")
    row["missing"]={label:missing.get(label,"not advertised") for label in selected if label not in owners}
    root=Path(cmd(["bazel","info","execution_root"]))
    for label,owner in owners.items():
        try:
            if label!=hook and label not in executables:
                single=subprocess.run(["bazel","cquery",*args,"--output=starlark","--starlark:expr="+expr,label],capture_output=True,text=True)
                if not single.returncode and "\t" in single.stdout.strip():
                    executables[label]=single.stdout.strip().split("\t",1)[1]
            advertised=report.advertised_build_outputs(events,{(owner,"default")})
            entries=[v for v in advertised if label==hook or v["name"]==executables.get(label)]
            (entry,)=entries
            path=root/entry["name"]
            check(report.file_record(path)=={"sha256":entry["sha256"],"bytes":entry["bytes"]},"materialized digest")
            v={"path":entry["name"],"configuration":owner.configuration,"sha256":entry["sha256"],"bytes":entry["bytes"],**image(path)}
            row["outputs"][label]=v
            if label==hook:
                v["glibc_versions"]=sorted(set(re.findall(r"\bGLIBC_[0-9.]+",cmd(["readelf","--version-info","-W",path]))))
            else:
                v["go_info"]=go_info(path)
            if label==labels[0]:
                v["launch"]=launch(path,entry["sha256"])
        except (ValueError,RuntimeError,OSError,subprocess.SubprocessError) as e:
            row.setdefault("verification_errors",[]).append([label,type(e).__name__,str(e)[:180]])
    if hook in row["outputs"] and labels[2] in row["outputs"]:
        library=row["outputs"][hook]
        with (root/row["outputs"][labels[2]]["path"]).open("rb") as f, mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as main:
            library["offset_in_run_sniffer"]=main.find((root/library["path"]).read_bytes())
    try:
        if execution.is_file():
            physical(execution,row["outputs"])
    except (ValueError,RuntimeError,OSError,subprocess.SubprocessError) as e:
        row.setdefault("verification_errors",[]).append(["physical",type(e).__name__,str(e)[:180]])
    row["finished_time"]=time.time();save()
    shared=(b"No space left on device",b"RESOURCE_EXHAUSTED",b"StatusRuntimeException: UNAVAILABLE",b"Failed to fetch repository",b"Error in download_and_extract",b"Failed to download",b"no such package '@@")
    return run.returncode and not row["outputs"] and (any(x in run.stdout for x in shared) or not owners)

def main():
    base,alternate=sources.values()
    check(cmd(["git","rev-parse",alternate+"^"])==base,"alternate ancestry")
    check(set(cmd(["git","diff","--name-only",base,alternate]).splitlines())==paths,"changed paths")
    for name in paths:
        before=cmd(["git","show",base+":"+name])
        after=cmd(["git","show",alternate+":"+name])
        check(before.count("static = True,")==1 and before.replace("static = True,", 'features = ["fully_static_link"],')==after,name)
    for variant,source in sources.items():
        subprocess.run(["git","checkout","--detach",source],check=True,stdout=subprocess.DEVNULL)
        check(cmd(["git","rev-parse","HEAD"])==source and not cmd(["git","diff","--name-only"]),"actual source")
        for mode in ("cgo","race"):
            if phase(variant,mode):
                facts["stopped"]="no selected artifacts after a build failure; remaining cells not submitted"
                return False
    # A cached or already-local link can be materialized without its spawn
    # appearing again. Record the byte-identical producer in its actual cell.
    links={}
    for row in facts["phases"]:
        for v in row["outputs"].values():
            if v.get("equal_byte_link_actions"):
                links[v["sha256"]]=[row["source"],row["mode"]]
    for row in facts["phases"]:
        for label,v in row["outputs"].items():
            v["witness_cell"]=([row["source"],row["mode"]] if v.get("equal_byte_link_actions") else links.get(v["sha256"]))
            if not v["witness_cell"]:
                row.setdefault("verification_errors",[]).append([label,"missing matching link"])
        v=row["outputs"].get(labels[0])
        if v and "launch" in v:
            ordinary=v["launch"]["ordinary"]
            bare=v["launch"]["empty_chroot"]
            if not ordinary["pcap_header"] or ordinary["exit"] or not ((bare.get("pcap_header") and bare.get("exit")==0) or (v["interpreter"] and bare.get("exec_errno")==2)):
                row.setdefault("verification_errors",[]).append([labels[0],"launch outcome",bare])
        if library:=row["outputs"].get(hook):
            if library.get("offset_in_run_sniffer",-1)<0:
                row.setdefault("verification_errors",[]).append([hook,"whole embedded hook not found"])
    return all(r["native_exit"]==0 and not r.get("verification_errors") and len(r["outputs"])==7 for r in facts["phases"])

if __name__=="__main__":
    try:
        ok=main()
        facts["status"]="PASS" if ok else "FAIL"
        if not ok: raise SystemExit(1)
    finally:
        save()
