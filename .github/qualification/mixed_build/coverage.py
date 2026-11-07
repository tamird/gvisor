"""Compare original non-test roots, their default products, and mixed ARM dependencies."""
from collections import Counter
import gc
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import traceback

from action_graph import graph


SOURCE = "4b87bd4875ecde1ab6b994030d59871b59363ca1"
W = Path(os.environ["RUNNER_TEMP"], "mixed-build")
OUT = W / "results"
BAZEL = [str(W/"bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx8g"]
PROFILES = {"amd64":"source-presubmit", "arm64":"source-presubmit-arm64", "mixed":"source-presubmit-mixed"}
FLAGS = ["--jobs=4","--keep_going","--noshow_progress","--color=no","--curses=no",
         "--remote_executor=","--remote_cache=","--bes_backend=","--spawn_strategy=local","--strategy=TestRunner=local"]
LABEL = re.compile(r"//[\w+./-]*:[\w+./-]+")
ORIG = set((W/"original.txt").read_text().splitlines())
RUNNABLE = json.loads((W/"runnable.json").read_text())
REPORT = {"source":SOURCE, "original_artifact":13550377161, "runnable_artifact":13553917910, "profiles":{}}
DETAIL = {}
FORMAT = '''
def files(group):
    return "|".join([("S" if f.is_source else "D") + f.path for f in group.to_list()])
def format(target):
    label = str(target.label).lstrip("@")
    if label not in originals:
        return ""
    opts = build_options(target) or {}
    if opts.get("//command_line_option:is exec configuration", False):
        return ""
    cpu = str(opts.get("//command_line_option:cpu", "none"))
    platforms = str(opts.get("//command_line_option:platforms", ""))
    ps = providers(target) or {}
    info = ps.get("DefaultInfo")
    groups = ps.get("OutputGroupInfo")
    rf = info.default_runfiles if info else None
    # Include concrete runfile targets; special symlink layouts stay opaque.
    syms = (len(rf.symlinks.to_list()) + len(rf.root_symlinks.to_list())) if rf else 0
    extras = []
    for name in ["_validation", "_validation_top_level", "_hidden_top_level_INTERNAL_"]:
        if groups and hasattr(groups, name):
            extras.append(files(getattr(groups, name)))
    nogo = files(groups.nogo_findings) if groups and hasattr(groups, "nogo_findings") else ""
    return "\\t".join([label,cpu,platforms,files(target.files),files(rf.files) if rf else "", "|".join(extras),nogo,str(syms)])
'''


def save():
    (OUT/"summary.json").write_text(json.dumps(REPORT,sort_keys=True)+"\n")


def cmd(name, kind, args):
    output, log, bep = (W/(name+s) for s in (".out",".log",".bep"))
    options=["--build_event_json_file="+str(bep)] if kind!="query" else []
    with output.open("wb") as stdout, log.open("w") as stderr:
        rc=subprocess.run(["timeout","--signal=INT","--kill-after=40s","25m",*BAZEL,kind,*args,*options],stdout=stdout,stderr=stderr).returncode
    errors=[]
    for l in log.read_text(errors="replace").splitlines():
        if l.startswith(("ERROR:","FATAL:","Caused by:")) and len(errors)<12:
            msg=re.sub(r"https?://\S+|/(?:home|tmp|opt|usr)/\S+", "<path>", l)[:400]
            if msg not in errors:errors.append(msg)
    return output,bep,{"exit":rc,"diagnostics":errors}


def query_file(name, expr):
    p=W/(name+".query")
    p.write_text(expr+"\n")
    return "--query_file="+str(p)


def target_set(labels):
    return "set("+" ".join(sorted(labels))+")"


def select(mode, flags, summary):
    _,bep,probe=cmd(mode+"-all","test",[*flags,"--nobuild"])
    selected={}; dispositions={}; suites=set(); events=Counter()
    if bep.exists():
        for line in bep.open():
            e=json.loads(line);i=e["id"]
            if e.get("configured",{}).get("targetKind")=="test_suite rule":
                suites.add(i["targetConfigured"]["label"])
            events.update(k for k in ("action","testResult","testSummary") if k in e)
            t=i.get("targetCompleted",{})
            if t and not t.get("aspect"):
                reason=e.get("aborted",{}).get("reason","completed")
                if reason=="NO_BUILD":selected[t["label"]]=t.get("configuration",{}).get("id")
                elif t["label"] in ORIG:dispositions[t["label"]]=reason
    for label in suites:selected.pop(label,None)
    running=set(RUNNABLE["ordinary"])
    if mode=="mixed":running.update(RUNNABLE["mixed_extra"])
    build=set(selected)-running
    probe.update(all=len(selected),build_only=len(build),runnable=len(set(selected)&running),
                 absent_runnable=sorted(running-set(selected)),missing=sorted(ORIG-build),
                 extra=sorted(build-ORIG),terminal_dispositions=dispositions,execution_events=dict(events))
    summary["selection"]=probe;save()
    assert probe["exit"]==1 and not events and selected
    return set(selected),build


def files(s):
    return [(v[0]=="S",v[1:]) for v in s.split("|") if v]


def records(mode, name, flags, expr, summary):
    path,_,r=cmd(mode+"-"+name,"cquery",[*flags,"--output=starlark",
        "--starlark:file="+str(W/"project.star"),query_file(mode+"-"+name,expr)])
    summary[name]=r;save()
    rows=[]
    with path.open() as stream:
        for line in stream:
            if not line.strip():continue
            owner,cpu,platforms,root,runfiles,validation,findings,syms=line.rstrip("\n").split("\t")
            arch="arm64" if "linux_arm64" in platforms or cpu in ("arm64","aarch64") else (
                  "amd64" if "linux_amd64" in platforms or cpu in ("k8","x86_64","amd64") else "other")
            assert owner in ORIG
            findings=files(findings)
            rows.append((owner,arch,files(root),files(runfiles)+files(validation),
                         sorted({a for _,p in findings for a in ("amd64","arm64") if p.endswith("."+a+".findings")}),int(syms)))
    summary[name]["rows"]=len(rows);save()
    assert r["exit"]==0 and rows
    return rows


def combine(mode, raw, inspect):
    result={}
    for owner,arch,defaults,additional,nogo,syms in raw:
        base=inspect(sorted(set(defaults)))
        all_files=inspect(sorted(set(defaults+additional)))
        runfile_findings=sorted({a for _,path in additional for a in ("amd64","arm64")
                                 if path.endswith("."+a+".findings")})
        value={"cpu":arch,"default":base,"products":all_files,"nogo":nogo,
               "nogo_runfiles":runfile_findings if nogo else [],"runfile_symlinks":syms}
        if value not in result.setdefault(owner,[]):result[owner].append(value)
    return result


def one(mode):
    summary=REPORT["profiles"].setdefault(mode,{})
    flags=["--config="+PROFILES[mode],*FLAGS]
    selected,build=select(mode,flags,summary)
    roots=target_set(build&ORIG)
    top=records(mode,"top",flags,roots,summary)
    # Keep target configurations reached through the ordinary test transitions.
    # Exclude external actions to bound the archive of the analysis. Unexpanded
    # target-side dependencies are explicitly marked opaque in the result.
    closure='filter("^(@@)?//",deps('+target_set(selected)+'))'
    indirect=records(mode,"deps",flags,closure,summary) if mode=="mixed" else []
    path,bep,ar=cmd(mode+"-actions","aquery",[*flags,"--output=streamed_proto","--include_artifacts",
        "--include_commandline","--include_param_files","--include_file_write_contents",
        query_file(mode+"-actions",closure)])
    summary["actions"]=ar;save()
    if bep.exists():
        with bep.open() as stream:
            ar["execution_events"]=sum(any(k in e for k in ("action","testResult","testSummary"))
                for line in stream for e in [json.loads(line)])
    subprocess.run([*BAZEL,"shutdown"],check=False,timeout=90)
    save()
    assert ar["exit"]==0 and ar.get("execution_events")==0 and path.stat().st_size
    inspect=graph(path)
    top_values=combine(mode,top,inspect)
    all_values=combine(mode,indirect,inspect)
    summary["observed_top_owners"]=len(top_values)
    summary["unprojected_top_owners"]=sorted((build&ORIG)-top_values.keys())
    path.unlink()
    del inspect
    gc.collect()
    save()
    return top_values,all_values


def classify(a,b):
    if not a or not b:return "no-unique-default-product"
    if a["nogo"]==b["nogo"]==a["nogo_runfiles"]==b["nogo_runfiles"]==["amd64","arm64"]:return "nogo-already-splits-both-targets"
    x,y=a["products"],b["products"]
    if x["outputs"]==y["outputs"]==0 and not (a["runfile_symlinks"] or b["runfile_symlinks"]):
        return "empty-default-product"
    if x["source_only"] and y["source_only"] and x["recipe"]==y["recipe"] and not (a["runfile_symlinks"] or b["runfile_symlinks"]):
        return "same-source-files"
    if x["recipe"]!=y["recipe"]:
        if x["native_cpu_flags"]==["amd64"] and y["native_cpu_flags"]==["arm64"]:
            return "distinct-native-target-machine"
        if x["native"] or y["native"]:return "different-recipe-with-compiled-product"
        if x["product_names"]!=y["product_names"]:return "different-selected-products"
        return "different-generated-product-recipe"
    if x["native"] or y["native"]:return "matching-compile-command"
    if x["opaque"] or y["opaque"] or a["runfile_symlinks"] or b["runfile_symlinks"]:
        return "matching-partially-observed-recipe"
    return "same-target-inputs-and-producer-recipe"


def main():
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==SOURCE
    assert len(ORIG)==2880 and len(RUNNABLE["ordinary"])==363
    assert all(LABEL.fullmatch(s) for s in ORIG)
    definition="originals = "+repr(dict.fromkeys(sorted(ORIG),True))+"\n"
    (W/"project.star").write_text(definition+FORMAT)
    out,_,probe=cmd("original-kinds","query",["--output=label_kind",query_file("kinds",target_set(ORIG))])
    kinds={}
    for line in out.read_text().splitlines():
        kind,_,label=line.rpartition(" ")
        kinds[label]=kind.removesuffix(" rule")
    REPORT["kinds"]={**probe,"count":len(kinds)}
    save()
    assert probe["exit"]==0 and kinds.keys()==ORIG
    tops={}; reach={}
    for mode in PROFILES:
        try:
            tops[mode],reached=one(mode)
            if mode=="mixed":reach=reached
            (OUT/(mode+"-products.json")).write_text(json.dumps(tops[mode],sort_keys=True,separators=(",",":"))+"\n")
            if mode=="mixed":
                (OUT/"mixed-dependencies.json").write_text(json.dumps(reach,sort_keys=True,separators=(",",":"))+"\n")
        except Exception as e:
            REPORT["profiles"][mode]["error"]={"type":type(e).__name__,
                "locations":[[Path(x.filename).name,x.lineno] for x in traceback.extract_tb(e.__traceback__)],
                "message":re.sub(r"(https?://\S+|/(?:home|tmp|opt|usr)/\S+)", "<path>", str(e))[:250]}
            save()
            subprocess.run([*BAZEL,"shutdown"],check=False,timeout=90)
        gc.collect()
    assert tops.keys()==PROFILES.keys()
    classes=Counter();gaps=Counter();not_arm=Counter()
    with (OUT/"owners.jsonl").open("w") as stream:
        for label in sorted(ORIG):
            subjects={k:v[label][0] if len(v.get(label,[]))==1 else None for k,v in tops.items()}
            a,b,m=(subjects[mode] for mode in PROFILES)
            category=classify(a,b)
            mixed_arm=[d for d in reach.get(label,[]) if d["cpu"]=="arm64"]
            mixed_cpu=sorted({d["cpu"] for d in reach.get(label,[])})
            covered=bool(b and not b["runfile_symlinks"] and any(not d["runfile_symlinks"]
                and d["products"]["recipe"]==b["products"]["recipe"] for d in mixed_arm))
            fact={"label":label,"kind":kinds[label],"category":category,"top":subjects,
                  "mixed_dependency_cpus":mixed_cpu,"mixed_arm_observed_recipe_matches":covered,
                  "mixed_arm_variant_count":len(mixed_arm)}
            stream.write(json.dumps(fact,sort_keys=True,separators=(",",":"))+"\n")
            classes[category]+=1
            if not covered:gaps[category]+=1
            if not mixed_arm:not_arm[category]+=1
    REPORT.update(categories=classes,without_matching_observed_arm_recipe=gaps,without_arm_target_config=not_arm,
                  verified_all_profiles=True)
    save()


if __name__=="__main__":
    main()
