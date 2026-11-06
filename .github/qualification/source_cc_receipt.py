"""Report two original failing C actions, their declared compiler and their ELFs."""
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import uuid

from source_presubmit_results import cfg, digest_item, signal

SOURCE = "66243bc605cf17bcb190c4762031001b3ae95c3d"
TARGETS = {"//tools/cstate_wake_probe:cstate_wake_probe":"tools/cstate_wake_probe/cstate_wake_probe",
           "//runsc/fdparking:fdparking_binary":"runsc/fdparking/runsc-fd-parking"}
PLATFORM="//test/rbe:linux_amd64"
IMAGE="docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
TMP=Path(os.environ["RUNNER_TEMP"])
DEST=TMP/"source-cc-results"


def check_elf(path):
    data=path.read_bytes()
    assert data[:6]==b"\x7fELF\x02\x01"
    etype,machine=struct.unpack_from("<HH",data,16)
    off=struct.unpack_from("<Q",data,32)[0]
    entsize,num=struct.unpack_from("<HH",data,54)
    assert machine==62 and etype in (2,3) and entsize>=56 and off+entsize*num<=len(data)
    ptypes=[struct.unpack_from("<I",data,off+i*entsize)[0] for i in range(num)]
    return {"sha256":hashlib.sha256(data).hexdigest(),"size":len(data),"machine":machine,
            "type":etype,"interp":3 in ptypes,"dynamic":2 in ptypes,"executable":os.access(path,os.X_OK)}


def collect_plan():
    fields={"analysis_exit":None,"actions":[]}
    args=["bazelisk","aquery","--config=source-presubmit","--spawn_strategy=local","--jobs=3","--output=jsonproto",
          "--include_commandline","--include_artifacts=false",
          'mnemonic("^(CppCompile|CppLink|Genrule)$", set('+" ".join(TARGETS)+"))"]
    with (TMP/"source-cc-plan.json").open("w") as out, (TMP/"source-cc-plan.log").open("w") as err:
        r=subprocess.run(args,stdout=out,stderr=err)
    fields["analysis_exit"]=r.returncode
    if r.returncode==0:
        graph=json.loads((TMP/"source-cc-plan.json").read_text())
        names={str(t["id"]):t["label"] for t in graph["targets"]}
        for a in graph.get("actions",[]):
            label=names[str(a["targetId"])]
            if label not in TARGETS:
                continue
            props={p["key"]:p.get("value","") for p in a.get("executionInfo",[])}
            argv=" ".join(a.get("arguments",[]))
            fields["actions"].append({"owner":label,"mnemonic":a["mnemonic"],
                "platform_ok":a.get("executionPlatform") in (PLATFORM,"@@"+PLATFORM),
                "image_ok":props.get("container-image")==IMAGE,"oci":props.get("workload-isolation-type")=="oci",
                "amd64":props.get("Arch")=="amd64","clang":bool(re.search(r"\bclang(?:\.\+\+|\+\+)?\b",argv)),
                "llvm_path":bool(re.search(r"(?:external|bin)/[^ ]*(?:llvm|clang)",argv)),
                "undeclared_gcc":"/usr/bin/x86_64-linux-gnu-gcc" in argv or bool(re.search(r"(?<![-/\w])x86_64-linux-gnu-gcc\b",argv))})
    (DEST/"plan.json").write_text(json.dumps(fields,indent=2)+"\n")


def collect_result():
    summary={"source":SOURCE,"status":{},"actions":[],"failures":[],"outputs":{},"signals":[]}
    rc=TMP/"source-cc-exit"
    summary["process_exit"]=int(rc.read_text()) if rc.exists() else None
    bep=TMP/"source-cc.bep"
    if bep.is_file():
        with bep.open() as raw:
            for line in raw:
                try:
                    e=json.loads(line)
                except json.JSONDecodeError:
                    summary["truncated_bep"]=True
                    break
                ident=e.get("id",{})
                if "started" in e and e["started"].get("uuid"):
                    summary["invocation"]=str(uuid.UUID(e["started"]["uuid"]))
                if "finished" in e:
                    summary["bazel_exit"]=int(e["finished"].get("exitCode",{}).get("code",0))
                if "targetCompleted" in ident and ident["targetCompleted"].get("label") in TARGETS and not ident["targetCompleted"].get("aspect"):
                    key=ident["targetCompleted"]["label"]
                    summary["status"][key]=bool(e.get("completed",{}).get("success",False))
                if "action" in e:
                    a=e["action"];identity=ident.get("actionCompleted",{});owner=identity.get("label")
                    mnemonic=a.get("type","other")
                    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,65}",mnemonic):
                        mnemonic="other"
                    item={"owner":owner if owner in TARGETS else "dependency","mnemonic":mnemonic,
                          "exit":int(a.get("exitCode",0)),"success":bool(a.get("success",False)),
                          "stderr":digest_item(a.get("stderr",{})),"config":cfg(identity.get("configuration",{}).get("id"))}
                    if owner in TARGETS:
                        summary["actions"].append(item)
                    if not item["success"] and len(summary["failures"])<20:
                        item["signal"]=signal(str(a.get("failureDetail",{})))
                        summary["failures"].append(item)
    for owner, rel in TARGETS.items():
        path=Path("bazel-bin")/rel
        if path.is_file():
            summary["outputs"][owner]=check_elf(path)
    for log in (TMP/"source-cc.log",TMP/"source-cc-plan.log"):
        if log.is_file():
            with log.open(errors="replace") as lines:
                for line in lines:
                    item=signal(line)
                    if item and item not in summary["signals"] and len(summary["signals"])<30:
                        summary["signals"].append(item)
    summary["built_both"]=summary.get("bazel_exit")==0 and summary["process_exit"]==0 and all(summary["status"].get(k) and summary["outputs"].get(k,{}).get("executable") for k in TARGETS)
    (DEST/"build.json").write_text(json.dumps(summary,indent=2)+"\n")


assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==SOURCE
DEST.mkdir(exist_ok=True)
if sys.argv[1]=="plan":
    collect_plan()
else:
    collect_result()
