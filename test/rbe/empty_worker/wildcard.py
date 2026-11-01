"""Reconcile the production package wildcard and its observed worker actions."""
import collections
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "package-evidence"
out.mkdir(exist_ok=True)
source = os.environ["PRODUCT"]
image = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
old = json.loads((tmp / "ubuntu-expected.json").read_text())
scope = "//pkg/..."
bzl = ["bazel", "--bazelrc=" + str(tmp / "sidecar.bazelrc")]
def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
def write(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")
def digest(lines):
    return hashlib.sha256(("\n".join(sorted(lines)) + "\n").encode()).hexdigest()
def events(path):
    result = {"expanded":set(), "configured":{}, "completed":{}, "finish":{}, "invocation":None}
    if not path.exists():
        return result
    with path.open() as stream:
        for line in stream:
            try:
                e = json.loads(line)
            except ValueError:
                result["partial_final_record"] = True
                break
            ident=e["id"]
            if "started" in e:
                result["invocation"]=e["started"].get("uuid")
            if ident.get("pattern",{}).get("pattern")==[scope]:
                result["expanded"].update(v["targetConfigured"]["label"] for v in e.get("children",[]) if "targetConfigured" in v)
            if "targetConfigured" in ident and "aspect" not in ident["targetConfigured"]:
                lab=ident["targetConfigured"]["label"]
                result["configured"][lab]=[v["targetCompleted"].get("configuration",{}).get("id") for v in e.get("children",[]) if "targetCompleted" in v and "aspect" not in v["targetCompleted"]]
            if "targetCompleted" in ident and "aspect" not in ident["targetCompleted"]:
                t=ident["targetCompleted"]
                sig=(t["label"],t.get("configuration",{}).get("id"))
                result["completed"][sig]="built" if e.get("completed",{}).get("success") else e.get("aborted",{}).get("reason","FAILED")
            if "buildFinished" in ident:
                f=e.get("finished",{})
                result["finish"]={k:f.get(k) for k in ("overallSuccess","exitCode","finishTime")}
    return result

def diagnostics(file, folder):
    errors=[]
    rows=file.read_text(errors="replace").splitlines()
    for i,line in enumerate(rows):
        if line.startswith(("ERROR:","panic:","FATAL:")) or "not found" in line.lower() or "no such file" in line.lower():
            block="\n".join(rows[i:i+9])[:2600]
            if not re.search(r"api[-_]?key|authorization|credential|remote_header|bes_header|token=",block,re.I) and len(errors)<36:
                errors.append(block)
    (folder/"errors.txt").write_text("\n---\n".join(errors)+"\n")
    return errors[:2]

def stats(log, arch, folder):
    full = collections.Counter()
    examples = {}
    bad=[]
    # The log expander joins inputs using the full compact log. Project only
    # bounded public fields; never export the command arguments/environment.
    filt='select(has("mnemonic")) | {m:.mnemonic,l:.targetLabel,x:.commandArgs[0],r:.runner,c:.cacheHit,s:.status,e:.exitCode,d:.digest,p:(.platform.properties // [])}'
    if not log.is_file():
        return {"available":False}
    with subprocess.Popen([str(tmp/"bb"),"print","--compact_execution_log="+str(log)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as decode:
        with subprocess.Popen(["jq","-c",filt],stdin=decode.stdout,stdout=subprocess.PIPE,text=True) as lines:
            decode.stdout.close()
            for line in lines.stdout:
                row=json.loads(line)
                props={p["name"]:p["value"] for p in row["p"]}
                environment={k:props.get(k) for k in ("Arch","container-image","workload-isolation-type")}
                seen = props.get("Arch")==arch and props.get("container-image")==image and props.get("workload-isolation-type")=="oci"
                runner=row["r"] or "unknown"
                key=(row["m"],runner,bool(row["c"]),seen,row["x"] or "")
                full[key]+=1
                sample={k:row[k] for k in ("l","d","s","e")}
                if key not in examples: examples[key]=sample
                if row["e"] or row["s"] or (runner not in ("remote cache hit","remote") or not seen):
                    if len(bad)<160: bad.append(dict(sample,executable=row["x"],runner=runner,platform=environment))
            jq_rc=lines.wait()
        reader_rc=decode.wait()
    write(folder/"actions.json",{"groups":[dict(mnemonic=m,runner=r,cache_hit=c,correct_platform=ok,tool=x,count=n,example=examples[(m,r,c,ok,x)]) for (m,r,c,ok,x),n in full.items()], "failed_or_other":bad})
    total=sum(full.values())
    return {"available":True,"decoder_exits":[reader_rc,jq_rc],"total":total,
            "observed_platform":sum(n for (m,r,c,ok,x),n in full.items() if ok),
            "remote":sum(n for (m,r,c,ok,x),n in full.items() if r=="remote" and not c and ok),
            "cached_on_platform":sum(n for (m,r,c,ok,x),n in full.items() if c and ok),
            "off_platform":sum(n for (m,r,c,ok,x),n in full.items() if not ok),
            "local_runs":sum(n for (m,r,c,ok,x),n in full.items() if not c and r!="remote")}

def collect(arch):
    folder=out/arch
    folder.mkdir(exist_ok=True)
    expected=set(old[arch])
    row = {"arch":arch,"ubuntu_baseline":len(expected),"ubuntu_sha256":digest(expected),"phases":{}}
    plat="//test/rbe/empty_worker:"+arch
    common=["--config="+{"amd64":"x86_64","arm64":"aarch64"}[arch],"--compilation_mode=fastbuild",
            "--host_platform="+plat,"--extra_execution_platforms="+plat,"--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
            "--extra_toolchains=//test/rbe/go_bootstrap:linux_"+arch+"_ash","--remote_accept_cached","--noremote_upload_local_results",
            "--jobs=40","--remote_download_outputs=minimal","--noremote_local_fallback","--spawn_strategy=remote","--keep_going","--color=no","--curses=no"]
    census=None
    for phase in ("analysis","build"):
        bep=tmp/(arch+"-"+phase+".bep")
        log=tmp/(arch+"-"+phase+".txt")
        compact=tmp/(arch+".compact")
        flags=["--nobuild"] if phase=="analysis" else ["--execution_log_compact_file="+str(compact)]
        args=["build",*common,*flags,"--build_event_json_file="+str(bep),scope]
        started=now()
        print(arch,phase,started,flush=True)
        with log.open("w") as stream:
            rc=subprocess.run(["timeout","--signal=INT","--kill-after=25s","12m" if phase=="analysis" else "43m",*bzl,*args],stdout=stream,stderr=subprocess.STDOUT).returncode
        end=now()
        parsed=events(bep)
        expanded=parsed["expanded"]
        completed=parsed["completed"]
        record={"args":args,"started":started,"finished":end,"exit":rc,"invocation":parsed["invocation"],"finish":parsed["finish"],
                "expanded":len(expanded),"expanded_sha256":digest(expanded),"configured":len(parsed["configured"]),
                "added_vs_ubuntu":sorted(expanded-expected),"removed_vs_ubuntu":sorted(expected-expanded)}
        if not expanded:
            record["removed_vs_ubuntu"]=[]
        row["phases"][phase]=record
        if phase=="analysis":
            census=expanded
            write(folder/"expected.json",sorted(census))
        if phase=="build":
            successes={l for (l,c),v in completed.items() if v=="built"}
            needed={(l,c) for l,configs in parsed["configured"].items() if l in expanded for c in configs}
            missing_config=expanded-parsed["configured"].keys()
            missing=needed-completed.keys()
            dispositions={lab:[(cfg,completed.get((lab,cfg),"MISSING")) for cfg in configs] for lab,configs in parsed["configured"].items() if lab in expanded}
            write(folder/"completed.json",dispositions)
            record.update(skipped_labels=sorted(l for (l,c),v in completed.items() if v=="SKIPPED"),agree_analysis=(bool(census) and census==expanded),successful=len(successes & expanded),missing_configured=sorted(missing_config),
                          missing_completion=sorted(missing),dispositions=dict(collections.Counter(completed.get(k,"MISSING") for k in needed)),
                          without_configuration=sorted(l for l,v in dispositions.items() if not v))
        record["first_errors"]=diagnostics(log,folder)
        write(folder/"receipt.json",row)
        if phase=="build":
            record["actions"]=stats(compact,arch,folder)
        write(folder/"receipt.json",row)
        print(arch,phase,rc,len(expanded),record.get("dispositions"),record.get("actions"),record.get("first_errors",[])[:1],flush=True)
        if phase=="analysis" and rc:
            break
    return row

baseline="f906c5dd87aeffc3a6f5cec8275b6102a278ea0a"
summary={"source":source,"release_base":baseline,"sniffer_fix":"1b96205bdc","target":scope,"image":image,
         "existing_root_controls":{"amd64":13056457271,"arm64":13061034514},"declared_go_shell":"existing opt-in plus CPU-selected BusyBox",
         "client":"Ubuntu AMD64; repository fetching and Bazel analysis occur on the client","architectures":{}}
subprocess.run(["git","checkout","--detach",source],check=True)
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==source
for arch in ("amd64","arm64"):
    try:
        row=collect(arch)
        section=row["phases"].get("build",{})
        a=section.get("actions",{})
        counts=section.get("dispositions",{})
        valid_changes=("//pkg/sentry/platform/systrap/sysmsg:","//pkg/seccomp/precompiledseccomp:","//pkg/test/capabilitytest:")
        changes=section.get("added_vs_ubuntu",[])+section.get("removed_vs_ubuntu",[])
        row["unexpected_package_changes"]=[x for x in changes if not x.startswith(valid_changes)]
        expected_skips=2 if arch=="arm64" else 0
        row["successful_observation"]=bool(not row["unexpected_package_changes"] and counts.get("SKIPPED",0)==expected_skips
                                  and section.get("skipped_labels")==([] if arch=="amd64" else ["//pkg/sentry/platform/slimvm:slimvm","//pkg/sentry/platform/slimvm/testutil:testutil"]) and section.get("agree_analysis") and section.get("exit")==0 and section.get("finish",{}).get("overallSuccess")
                                  and not (set(counts)-{"built","SKIPPED"}) and counts.get("built",0)>1800 and not section["without_configuration"]
                                  and not section["missing_configured"] and not section["missing_completion"]
                                  and a.get("decoder_exits")==[0,0] and a.get("observed_platform",0)>0 and not a["off_platform"] and not a["local_runs"])
        summary["architectures"][arch]=row
    except Exception as err:
        summary["architectures"][arch]={"error":repr(err)[:1400]}
    summary["updated"]=now()
    write(out/"summary.json",summary)
summary["status"]="PASS" if all(v.get("successful_observation") for v in summary["architectures"].values()) else "REVIEW_OR_INCOMPLETE"
summary["verified_at"]=now()
write(out/"summary.json",summary)
print(summary["status"],flush=True)
sys.exit(summary["status"]!="PASS")
