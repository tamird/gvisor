"""Build only Sentry outputs that were absent from the original target records."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

from original import CLI, MACHINE, NAMES, SOURCE, WORK, elf, summary


def write(name, record):
    (WORK/(name+".json")).write_text(json.dumps(record,indent=2)+"\n")


def native():
    record={"machine":platform.machine(),"kernel":platform.release(),"source":SOURCE,"cases":{}}
    assert record["machine"]=="aarch64"
    for name,(h,size) in CLI.items():
        path=WORK/name
        row=record["cases"][name]={"sha256":h,"executed":False}
        if not path.is_file():
            continue
        data=path.read_bytes()
        if len(data)!=size or hashlib.sha256(data).hexdigest()!=h or not elf(data,183):
            continue
        try:
            p=subprocess.run([str(path),"--version"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=45,
                             env={"PATH":"/usr/bin:/bin","LANG":"C","HOME":os.environ["HOME"]})
            output=(p.stdout+p.stderr).decode(errors="replace")
            row.update(executed=True,exit=p.returncode,runsc=bool(re.search(r"(?im)^runsc version\b",output)),
                       diagnostics=[v for v in ("ThreadSanitizer","unexpected memory mapping","unsupported VMA range","failed to mmap","Segmentation fault","Illegal instruction") if v.lower() in output.lower()])
        except (OSError,subprocess.TimeoutExpired) as exc:
            row["execution_error"]=type(exc).__name__
        write("arm-native",record)
    write("arm-native",record)


def execute(arch):
    assert arch in MACHINE
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==SOURCE
    assert not subprocess.check_output(["git","diff","--name-only","HEAD"])
    orig=json.loads((WORK/"original.json").read_text())["original"].get(arch,{}).get("targets",{})
    record={"source":SOURCE,"machine":MACHINE[arch],"targets":{}, "from_original":[]}
    labels=[]
    for target in NAMES:
        if orig.get(target,{}).get("elf"):
            record["from_original"].append(target)
            record["targets"][target]=orig[target]
        else:
            labels.append(target)
            record["targets"][target]={"completed":False}
    record["requested"]=labels
    name="sentry-"+arch
    write(name,record)
    if not labels:
        record["verified"]=True
        write(name,record)
        return

    log=WORK/(name+".log")
    bep=WORK/(name+".bep")
    with log.open("w") as out:
        result=subprocess.run(["timeout","--signal=INT","--kill-after=60s","74m",str(WORK/"bazel"),
            "--nosystem_rc","--nohome_rc","--bazelrc="+str(WORK/"client.rc"),"build",
            "--config="+{"arm64":"aarch64","amd64":"x86_64"}[arch],"-c","opt","--jobs=20","--keep_going",
            "--remote_download_outputs=toplevel","--build_event_json_file_path_conversion=false",
            "--build_event_json_file="+str(bep),"--color=no","--curses=no",*labels],stdout=out,stderr=subprocess.STDOUT)
    record["exit"]=result.returncode
    write(name,record)
    sets={}
    refs={}
    failures=[]
    if bep.exists():
        with bep.open() as stream:
            for line in stream:
                try:
                    event=json.loads(line)
                except ValueError:
                    continue
                ids=event.get("id",{})
                if "started" in event:
                    record["invocation"]=event["started"].get("uuid")
                if "action" in event and event["action"].get("success") is False and len(failures)<10:
                    act=event["action"]
                    owner=ids.get("actionCompleted",{}).get("label","")
                    code=act.get("exitCode")
                    stderr=act.get("stderr",{})
                    match=re.search(r"/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/(\d+)$",stderr.get("uri",""))
                    if re.fullmatch(r"(?:@@?[A-Za-z0-9_+.~/-]+)?//[A-Za-z0-9_/+.:-]+",owner):
                        failures.append({"owner":owner,"mnemonic":act.get("type","")[:44],"exit":code,
                                         "stderr_digest":match[1] if match else None, "stderr_bytes":int(match[2]) if match else None})
                if "namedSet" in ids:
                    sets[ids["namedSet"]["id"]]=event.get("namedSetOfFiles",{})
                t=ids.get("targetCompleted",{})
                if t.get("label") not in labels or t.get("aspect"):
                    continue
                completed=event.get("completed",{})
                target=t["label"]
                record["targets"][target]={"completed":completed.get("success",False)}
                refs[target]=[fs["id"] for g in completed.get("outputGroup",[]) if g["name"]=="default" for fs in g.get("fileSets",[])]
    record["failed_actions"]=failures
    with log.open("rb") as stream:
        first=stream.read(2*1024*1024)
        stream.seek(max(len(first),log.stat().st_size-3*1024*1024))
        last=stream.read(3*1024*1024)
    record["diagnostics"]=summary((first+b"\n"+last).decode(errors="replace"))
    for target,ids in refs.items():
        row=record["targets"][target]
        if not row.get("completed"):
            continue
        paths=set()
        seen=set()
        while ids:
            ref=ids.pop()
            if ref in seen:
                continue
            seen.add(ref)
            item=sets.get(ref,{})
            ids.extend(i["id"] for i in item.get("fileSets",[]))
            for f in item.get("files",[]):
                if Path(f.get("name","")).name!=NAMES[target]:
                    continue
                uri=urlsplit(f.get("uri",""))
                p=(Path(unquote(uri.path)) if uri.scheme=="file" else Path(*f.get("pathPrefix",[]),f["name"]))
                if p.is_file() and p.name==NAMES[target]:
                    paths.add(p)
        row["outputs"]=len(paths)
        if len(paths)==1:
            p,=paths
            with p.open("rb") as f:
                hdr=f.read(65)
                digest=hashlib.sha256(hdr)
                while chunk:=f.read(1024*1024):
                    digest.update(chunk)
            row.update(size=p.stat().st_size,sha256=digest.hexdigest(),machine=int.from_bytes(hdr[18:20],"little"),elf=elf(hdr,MACHINE[arch]))
    record["verified"]=record["exit"]==0 and all(r.get("elf") for r in record["targets"].values())
    write(name,record)


if __name__=="__main__":
    if sys.argv[1]=="native":
        native()
    else:
        execute(sys.argv[1])
