"""Collect the subordinate TLDK Make log for the original failed toolchain action."""

from pathlib import Path
import hashlib
import json
import os
import re
import traceback

import native_cross_inputs as ev

SOURCE = "cad667dcd75b15f549be5d4eeb23406433d7faa5"
TARGET = "@libpluginstack//:libpluginstack"
OWNER = "@@+http_archive+libpluginstack//:libpluginstack"
CONFIG = "bf5720485807a6dc7b4365463bdb87f0e6a79b098e76074418b9d9bf5f3b2960"
OUTPUT = "bazel-out/k8-opt/bin/external/+http_archive+libpluginstack/libpluginstack/include"
FLAGS = ["--config=x86_64", "-c", "opt", "--jobs=4", "--remote_cache=", "--remote_executor=",
         "--disk_cache=", "--config=plugin-tldk"]
D = Path(os.environ["RUNNER_TEMP"]) / "tldk-llvm"
OUT = D / "diagnostics"
R = {"source": SOURCE, "carrier": os.environ["CARRIER"], "requested": [TARGET], "status": "INCOMPLETE",
     "original_run": 42108207483, "original_artifact": 13454934734,
     "original_sha256": "ca5b58dee4f52af98f377c667c0a19e78bdbbc811b073c5cc2a82140a1551444"}


def digest(b):
    return hashlib.sha256(b).hexdigest()


def save():
    (OUT / "receipt.json").write_text(json.dumps(R, indent=2) + "\n")


def run(args, timeout):
    return ev.command([str(D / "bazel"), *args], timeout)


CREDENTIAL = re.compile(r'(?i)authorization\s*[:=]|credential\.|secret(?:_access)?_key|bearer\s+|x-buildbuddy-api-key|gh[pousr]_[A-Za-z0-9]{18,}|github_pat_|(?:token|passw(?:or)?d)\s*=')
FAIL = re.compile(r'(?i)(?:fatal )?error:|ld\.lld: (?:error|warning):|\*\*\*|no such file|permission denied|no rule to make|undefined reference|segmentation fault|core dumped|not found|killed|not recognized|cannot (?:open|find|execute|create)|no space left|not supported')


def retain(raw, source):
    if not raw.strip():
        return
    text = raw.decode(errors="replace")
    lines = text.splitlines()
    cleaned = []
    hidden = 0
    for line in lines:
        if CREDENTIAL.search(line):
            cleaned.append("[redacted credential-bearing line]")
            hidden += 1
            continue
        line = re.sub(r"https?://[^\s'\"]+", "<url>", line)
        cleaned.append(line)
    indices = [n for n,line in enumerate(cleaned) if FAIL.search(line)]
    # Preserve the entire small subordinate stream. If it is large, retain
    # every first failure with context, and the final commands that failed.
    encoded = "\n".join(cleaned).encode() + b"\n"
    complete = len(encoded) <= 700000
    if not complete:
        keep = set(range(min(5,len(cleaned)))) | set(range(max(0,len(cleaned)-120),len(cleaned)))
        for i in indices[:80]:
            keep.update(range(max(0,i-2),min(len(cleaned),i+3)))
        encoded = "\n".join(f"{i+1}: {cleaned[i][:1600]}" for i in sorted(keep)).encode()+b"\n"
    assert len(encoded) < 1000000 and not CREDENTIAL.search(encoded.decode())
    (OUT / "foreign-make.txt").write_bytes(encoded)
    R["foreign_make"]={"source":source, "original_bytes":len(raw),"original_sha256":digest(raw),
              "retained_bytes":len(encoded),"retained_sha256":digest(encoded),"complete_sanitized":complete,
              "redacted_lines":hidden,"error_line_numbers":[i+1 for i in indices[:20]]}
    save()


def main():
    assert ev.command(["git","rev-parse","HEAD"],20)[1].strip().decode()==SOURCE
    query='mnemonic("CcMakeMakeRule", '+TARGET+')'
    rc,raw,err=run(["aquery",*FLAGS,"--output=jsonproto",query],540)
    R["query_exit"]=rc;save()
    assert rc==0
    data=json.loads(raw)
    fragments={int(x["id"]):x for x in data["pathFragments"]}
    def path(i):
        x=fragments[int(i)]
        return (path(x["parentId"])+"/" if x.get("parentId") else "")+x["label"]
    files={int(x["id"]):path(x["pathFragmentId"]) for x in data["artifacts"]}
    labels={int(x["id"]):x["label"] for x in data["targets"]}
    configs={int(x["id"]):x["checksum"] for x in data["configuration"]}
    a,=[x for x in data["actions"] if x["mnemonic"]=="CcMakeMakeRule"]
    displayed=labels[int(a["targetId"])]
    assert displayed.lstrip("@") in (TARGET.lstrip("@"),OWNER.lstrip("@"))
    outputs={files[int(x)] for x in a["outputIds"]}
    logfile,=[x for x in outputs if x.endswith("_foreign_cc/Make.log")]
    R["query"]={"owner":displayed,"configuration":configs[int(a["configurationId"])],"action_key":a["actionKey"],
                "primary":files.get(int(a.get("primaryOutputId",0))),"outputs":sorted(outputs),"log":logfile}
    save()
    assert R["query"]["configuration"]==CONFIG and OUTPUT in outputs
    assert R["query"]["primary"] in (None,OUTPUT)
    bep=D/"archive.bep"
    rc,stdout,stderr=run(["build",*FLAGS,"--verbose_failures","--experimental_ui_max_stdouterr_bytes=8388608",
              "--build_event_publish_all_actions","--build_event_json_file="+str(bep),TARGET],4450)
    R.update(build_exit=rc,status="ARCHIVE_FAILED" if rc else "ARCHIVE_BUILT")
    save()
    # Bazel's sandbox runner copies declared outputs on failure as well. The
    # rules_foreign_cc wrapper also prints its log between these markers.
    root=Path(run(["info","execution_root"],60)[1].decode().strip())
    log=root/logfile
    if log.is_file(): retain(log.read_bytes(),"declared Make.log")
    if "foreign_make" not in R:
        text=stderr+b"\n"+stdout
        start,_,tail=text.partition(b"_____ BEGIN BUILD LOGS _____")
        if tail:
            body,mark,_=tail.partition(b"_____ END BUILD LOGS _____")
            if mark: retain(body,"foreign_cc wrapper transcript")
        if "foreign_make" not in R: retain(text,"Bazel console (no declared Make.log or complete wrapper block)")
    ev.ROOTS=[OWNER];ev.owned=lambda label: label==OWNER
    R["events"]=ev.events(bep)
    found=[x for x in R["events"]["actions"] if x["owner"]==OWNER and x["configuration"]==CONFIG and x["type"]=="CcMakeMakeRule" and x["output"]==OUTPUT]
    assert len(found)==1 and found[0]["success"]==(rc==0)
    assert "foreign_make" in R
    R["source_action_matched"]=True
    save()
    return rc


try:
    result=main()
except Exception as exc:
    R["reader_error"]={"type":type(exc).__name__,"lines":[x.lineno for x in traceback.extract_tb(exc.__traceback__)]}
    result=1
finally:
    save()
raise SystemExit(result)
