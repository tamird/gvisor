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
import time

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "package-evidence"
out.mkdir(exist_ok=True)
source = os.environ["PRODUCT"]
image = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
baseline = json.loads((tmp / "expected-targets.json").read_text())
old = baseline.get("expected", {})
scope = baseline["scope"]
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

def projection(raw):
    prefix = ".spawn | select(. != null) | " if raw else 'select(has("mnemonic")) | '
    args = ".args" if raw else ".commandArgs"
    return prefix + "{m:.mnemonic,l:.targetLabel,x:"+args+"[0],r:.runner,c:.cacheHit,s:.status,e:.exitCode,d:.digest,p:(.platform.properties // [])}"

def sample_projection(log, raw):
    started = time.monotonic()
    reader = [str(tmp/"bb"), "print", "--compact_execution_log="+str(log)]
    if raw:
        reader.append("--raw")
    # Ask jq to stop early so the legacy reader reconstructs just 32 spawns.
    # Closing the pipe then stops the printer; genuine decoder failures are
    # checked during the complete raw traversal in stats().
    with subprocess.Popen(reader, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        jq = 'limit(32; inputs | '+projection(raw)+')'
        with subprocess.Popen(["jq","-nc",jq], stdin=proc.stdout, stdout=subprocess.PIPE, text=True) as fields:
            proc.stdout.close()
            sample = [json.loads(line) for line in fields.stdout]
            if fields.wait():
                raise ValueError("could not read execution-log control")
        proc.wait()
    return sample, time.monotonic()-started

def stats(log, arch, folder):
    full = collections.Counter()
    examples = {}
    bad=[]
    # All fields in this projection are on the compact spawn itself.
    # Retain every spawn but avoid reconstructing its transitive inputs.
    filt=projection(True)
    if not log.is_file():
        return {"available":False}
    with subprocess.Popen([str(tmp/"bb"),"print","--raw","--compact_execution_log="+str(log)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as decode:
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
    fixed = old.get(arch)
    expected = set(fixed or [])
    row = {"arch":arch,"phases":{}}
    plat="//test/rbe/empty_worker:"+arch+"_musl"
    common=["--config="+{"amd64":"x86_64","arm64":"aarch64"}[arch],"--compilation_mode=fastbuild",
            "--host_platform="+plat,"--extra_execution_platforms="+plat,"--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
            "--extra_toolchains=//test/rbe/go_bootstrap:linux_"+arch+"_ash","--remote_accept_cached","--noremote_upload_local_results",
            "--jobs=40","--remote_download_outputs=minimal","--noremote_local_fallback","--spawn_strategy=remote","--keep_going","--color=no","--curses=no"]
    common.extend(baseline.get("extra_flags", []))
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
        if phase=="analysis" and fixed is None:
            expected=expanded
        record={"args":args,"started":started,"finished":end,"exit":rc,"invocation":parsed["invocation"],"finish":parsed["finish"],
                "expanded":len(expanded),"expanded_sha256":digest(expanded),"configured":len(parsed["configured"]),
                "added_vs_baseline":sorted(expanded-expected),"removed_vs_baseline":sorted(expected-expanded)}
        if not expanded:
            record["removed_vs_baseline"]=[]
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
        write(folder/"receipt.json",row)
        record["bep_parsed_at"] = now()
        try:
            record["first_errors"] = diagnostics(log,folder)
        except Exception as error:
            record["diagnostic_error"] = repr(error)[:1000]
        if phase == "build" and compact.is_file():
            record["compact_bytes"] = compact.stat().st_size
            record["projection_started"] = now()
            try:
                record["actions"] = stats(compact,arch,folder)
            except Exception as error:
                record["collection_error"] = repr(error)[:1000]
            record["projection_finished"] = now()
            write(folder/"receipt.json",row)
            try:
                new, raw_seconds = sample_projection(compact, True)
                old_sample, expanded_seconds = sample_projection(compact, False)
                record["projection_control"] = {"equal":len(new)==32 and new==old_sample,"count":len(new),
                        "raw_seconds": raw_seconds,"expanded_seconds": expanded_seconds}
            except Exception as error:
                record["control_error"] = repr(error)[:1000]
        record["verified_at"]=now()
        write(folder/"receipt.json",row)
        print(arch,phase,rc,len(expanded),record.get("dispositions"),record.get("actions"),record.get("first_errors",[])[:1],flush=True)
        if phase=="analysis" and rc and not census:
            break
    return row

def verdict(row, expected, skips):
    phases = row.get("phases", {})
    analysis = phases.get("analysis", {})
    build = phases.get("build", {})
    stats = build.get("actions", {})
    counts = build.get("dispositions", {})
    expected_counts = {"built": len(expected) - len(skips)}
    if skips:
        expected_counts["SKIPPED"] = len(skips)
    return bool(
        all(phase.get("exit") == 0 and phase.get("finish", {}).get("overallSuccess")
            and phase.get("expanded") == len(expected)
            and phase.get("expanded_sha256") == digest(expected)
            and not phase["added_vs_baseline"] and not phase["removed_vs_baseline"]
            for phase in (analysis, build))
        and build.get("agree_analysis")
        and build.get("successful") == len(expected) - len(skips)
        and set(build.get("skipped_labels", [])) == set(skips)
        and counts == expected_counts
        and not build["without_configuration"] and not build["missing_configured"]
        and not build["missing_completion"]
        and build.get("projection_control", {}).get("equal")
        and set(baseline.get("required_labels", [])) <= set(expected)
        and stats.get("decoder_exits") == [0, 0] and stats.get("total", 0) > 0
        and stats["total"] == stats["observed_platform"]
        and stats["total"] == stats["remote"] + stats["cached_on_platform"]
        and not stats["off_platform"] and not stats["local_runs"]
    )

summary = {"source": source, "scope": scope, "known_dispositions": baseline["provenance"],
           "image": image, "worker_reference": {"amd64": 13056457271, "arm64": 13061034514},
           "client": "Ubuntu AMD64 handles fetching and analysis; only action environments are checked here.",
           "architectures": {}}
subprocess.run(["git","checkout","--detach",source],check=True)
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip() == source
for arch in ("amd64", "arm64"):
    try:
        row = collect(arch)
        expected = old.get(arch) or json.loads((out/arch/"expected.json").read_text())
        row["successful_observation"] = verdict(row, expected, baseline["skipped"][arch])
        row["verified_at"] = now()
        summary["architectures"][arch] = row
    except Exception as error:
        summary["architectures"][arch] = {"error": repr(error)[:1400]}
    write(out / "summary.json", summary)
summary["source_clean"] = subprocess.run(["git","diff","--quiet",source,"--"]).returncode == 0
summary["status"] = "PASS" if summary["source_clean"] and all(v.get("successful_observation") for v in summary["architectures"].values()) else "REVIEW_OR_INCOMPLETE"
summary["verified_at"] = now()
write(out / "summary.json", summary)
print(summary["status"], flush=True)
sys.exit(summary["status"] != "PASS")
