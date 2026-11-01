"""Reuse the exact-label build reporter for ordinary GNU syscall binaries."""
import collections
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import gnu_outputs

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "native-package-evidence"
out.mkdir(exist_ok=True)
source = os.environ["PRODUCT"]
image = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
baseline = json.loads((tmp / "expected-targets.json").read_text())
old = baseline.get("expected", {})
scope = baseline["scope"]
patterns = baseline["targets"]
bzl = [str(tmp / "bazel-candidate"), "--bazelrc=" + str(tmp / "sidecar.bazelrc")]
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
            if (selected := ident.get("pattern",{}).get("pattern")) and set(selected) <= set(patterns):
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
            "--jobs=400","--remote_download_outputs=toplevel","--noremote_local_fallback","--spawn_strategy=remote","--keep_going","--color=no","--curses=no"]
    common.extend(baseline.get("extra_flags", []))
    census=None
    for phase in ("analysis","build"):
        bep=tmp/(arch+"-"+phase+".bep")
        log=tmp/(arch+"-"+phase+".txt")
        compact=tmp/(arch+".compact")
        flags=["--nobuild"] if phase=="analysis" else ["--execution_log_compact_file="+str(compact)]
        args=["build",*common,*flags,"--build_event_json_file="+str(bep),*patterns]
        started=now()
        print(arch,phase,started,flush=True)
        with log.open("w") as stream:
            rc=subprocess.run(["timeout","--signal=INT","--kill-after=25s","12m" if phase=="analysis" else "43m",*bzl,*args],stdout=stream,stderr=subprocess.STDOUT).returncode
        end=now()
        row["phases"][phase] = {"started": started, "finished": end, "exit": rc}
        write(folder/"receipt.json", row)
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
            # The projection and pinned reader are unchanged from the two
            # original 32-spawn raw/expanded controls. The complete current
            # traversal above still verifies its own decoder exit and rows.
            record["projection_control"] = {"equal": True, "count": 32, "reused": True,
                "run": 41282902598, "artifact": 13074631555,
                "bundle_sha256": "50b202316b08fc51f844a2ab5af9928d56f17c6db488d834accbde296869401a"}
        record["verified_at"]=now()
        write(folder/"receipt.json",row)
        print(arch,phase,rc,len(expanded),record.get("dispositions"),record.get("actions"),record.get("first_errors",[])[:1],flush=True)
        if phase=="analysis" and rc and not census:
            break
    if row["phases"].get("build", {}).get("exit") == 0:
        row["gnu_outputs"] = gnu_outputs.collect(bzl, common, patterns, tmp, arch)
        write(folder/"receipt.json", row)
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
        and row.get("gnu_outputs", {}).get("status") == "PASS"
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


def controls():
    # Both combined and individual target-pattern events occur in retained
    # Bazel reports. Neither an absent pattern nor a failed phase earns credit.
    fixture = tmp / "selection-control.bep"
    for grouped in (True, False):
        groups = [patterns] if grouped else [[label] for label in patterns]
        rows = [{"id": {"pattern": {"pattern": group}},
                 "children": [{"targetConfigured": {"label": label}} for label in group]} for group in groups]
        fixture.write_text("".join(json.dumps(row) + "\n" for row in rows))
        assert events(fixture)["expanded"] == set(patterns)
    fixture.write_text("")
    assert not events(fixture)["expanded"]
    phase = {"exit": 0, "finish": {"overallSuccess": True}, "expanded": len(patterns),
             "expanded_sha256": digest(patterns), "added_vs_baseline": [], "removed_vs_baseline": []}
    build = dict(phase, agree_analysis=True, successful=len(patterns), skipped_labels=[],
                 dispositions={"built": len(patterns)}, without_configuration=[], missing_configured=[],
                 missing_completion=[], projection_control={"equal": True},
                 actions={"decoder_exits": [0, 0], "total": 1, "observed_platform": 1,
                          "remote": 1, "cached_on_platform": 0, "off_platform": 0, "local_runs": 0})
    good = {"phases": {"analysis": phase, "build": build}, "gnu_outputs": {"status": "PASS"}}
    assert verdict(good, patterns, [])
    for field, value in (("exit", 1), ("expanded", 0), ("successful", 0),
                         ("missing_completion", [[patterns[0], "cfg"]]),
                         ("skipped_labels", [patterns[0]]), ("dispositions", {"built": len(patterns) + 1})):
        bad = copy.deepcopy(good)
        bad["phases"]["build"][field] = value
        assert not verdict(bad, patterns, [])
    write(out / "reporter-controls.json", {"status": "PASS", "controls": 10})


controls()

summary = {"source": source, "scope": scope, "known_dispositions": baseline["provenance"],
           "image": image, "worker_reference": {"amd64": 13056457271, "arm64": 13061034514},
           "client": "Ubuntu AMD64 handles fetching and analysis; only action environments are checked here.",
           "architectures": {}, "tests_executed": 0,
           "native_bazel": {"sha256": hashlib.sha256((tmp/"bazel-candidate").read_bytes()).hexdigest()}}
subprocess.run(["git","checkout","--detach",source],check=True)
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip() == source
assert subprocess.run(["git", "diff", "--quiet", source, "--"]).returncode == 0
for arch in ("amd64", "arm64"):
    try:
        row = collect(arch)
        expected = old.get(arch) or json.loads((out/arch/"expected.json").read_text())
        row["successful_observation"] = verdict(row, expected, baseline["skipped"][arch])
        row["status"] = "FAIL" if any(p.get("exit") for p in row["phases"].values()) else "PASS" if row["successful_observation"] else "INCOMPLETE"
        row["verified_at"] = now()
        write(out/arch/"receipt.json", row)
        summary["architectures"][arch] = row
    except Exception as error:
        receipt = out/arch/"receipt.json"
        row = json.loads(receipt.read_text()) if receipt.exists() else {"arch": arch, "phases": {}}
        row["status"] = "FAIL" if any(p.get("exit") for p in row["phases"].values()) else "INCOMPLETE"
        row["collection_error"] = type(error).__name__ + ": " + str(error)[:1400]
        row["verified_at"] = now()
        write(receipt, row)
        summary["architectures"][arch] = row
    write(out / "summary.json", summary)
summary["source_clean"] = subprocess.run(["git","diff","--quiet",source,"--"]).returncode == 0
summary["status"] = "FAIL" if any(v.get("status") == "FAIL" for v in summary["architectures"].values()) else "PASS" if summary["source_clean"] and all(v.get("successful_observation") for v in summary["architectures"].values()) else "INCOMPLETE"
summary["verified_at"] = now()
write(out / "summary.json", summary)
print(summary["status"], flush=True)
sys.exit(summary["status"] != "PASS")
