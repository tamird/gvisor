import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path.cwd()
PRODUCT = os.environ["SOURCE"]
OLD = "0eb8eca5ba25161133cd6fb8ad5a21604e988b92"
PARENT = "3c2172c0591f599949d87858215228cb1998b965"
BUNDLE = Path(os.environ["RUNNER_TEMP"]) / "gazelle-qualification"
BUNDLE.mkdir()
NAMES = ("gazelle-boundary-ordinary.txt", "gazelle-boundary-outside.txt", "gazelle-boundary-inside.txt")
rows = {}
expected = ("old_inputs", "fixed_inputs", "parent_consumer", "fixed_consumer", "parent_root", "fixed_root")
record = {"product": PRODUCT, "old": OLD, "prerequisite": PARENT, "rows": rows,
          "retained": "https://github.com/tamird/gvisor/actions/runs/41211321121/artifacts/13046890088"}
def now():
    return datetime.now(timezone.utc).isoformat()
def save():
    record["missing"] = [name for name in expected if name not in rows]
    (BUNDLE/"verdict.json").write_text(json.dumps(record, indent=2) + "\n")
def checkout(sha):
    subprocess.run(["git","checkout","--quiet","--detach",sha],cwd=SOURCE,check=True)
    assert subprocess.check_output(["git","rev-parse","HEAD"],cwd=SOURCE,text=True).strip()==sha
def bazel(row, cwd, args):
    flags=["--nosystem_rc","--nohome_rc"] if cwd==SOURCE else ["--ignore_all_rc_files"]
    result=subprocess.run(["bazel",*flags,*args],cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=510)
    errors=[l for l in result.stderr.splitlines() if l.startswith(("ERROR:", "Error in ", "FATAL:"))]
    print("BAZEL",args[0],cwd.name,result.returncode,now(),flush=True)
    row.setdefault("commands",[]).append({"command":args[:2],"exit":result.returncode,"errors":errors[-12:],"finished":now()})
    save()
    return result
def inputs(name,sha):
    checkout(sha)
    row=rows[name]={"sha":sha,"started":now()}
    res=bazel(row,SOURCE,["aquery","--output=jsonproto","--include_commandline","--include_artifacts","--platforms=//tools/bazeldefs:linux_amd64",'mnemonic("GazelleCheck", //:gazelle_check)'])
    res.check_returncode()
    g=json.loads(res.stdout)
    action,=g["actions"]
    fragments={str(f["id"]):f for f in g["pathFragments"]}
    def path(fid):
        f=fragments[str(fid)]
        if f.get("parentId") not in (None,0,"0"):
            return path(f["parentId"])/f["label"]
        return Path(f["label"])
    files={str(f["id"]):path(f["pathFragmentId"]) for f in g["artifacts"]}
    sets={str(s["id"]):s for s in g["depSetOfFiles"]}
    pending=[str(i) for i in action["inputDepSetIds"]];seen=set();declared=set()
    while pending:
        i=pending.pop()
        if i in seen: continue
        seen.add(i)
        entry=sets[i]
        declared.update(files[str(j)] for j in entry.get("directArtifactIds",[]))
        pending.extend(str(j) for j in entry.get("transitiveDepSetIds",[]))
    root,=[Path(a.split("=",1)[1]) for a in action["arguments"] if a.startswith("-repo_root=")]
    row["sentinels"]={n:(root/"pkg/bits"/n in declared) for n in NAMES}
    row["original_source_present"]=root/"pkg/bits/BUILD" in declared
    row["regular_digest"]=hashlib.sha256((SOURCE/"pkg/bits"/NAMES[0]).read_bytes()).hexdigest()
    row["projected_inputs"]=sum(p.is_relative_to(root) for p in declared)
    row["status"]="PASS" if row["original_source_present"] and list(row["sentinels"].values())==[True,sha==OLD,sha==OLD] else "FAIL"
    save()
def wildcard(name,sha,cwd,external):
    checkout(sha)
    row=rows[name]={"sha":sha,"started":now()}
    opts=["--ignore_dev_dependency","--override_module=gvisor="+str(SOURCE)] if external else []
    pat=("@gvisor" if external else "")+"//:"
    if external:
        # The unmodified consumer must be able to analyze the pattern before we
        # can attribute a newly broken public build to Gazelle.
        args=["build",*opts,"--platforms=@platforms//host","--nobuild",pat+"*"]
    else:
        # As root, exclude unrelated targets to isolate the developer tool and
        # its generated file. The original BUILD and module stay unchanged.
        labels=bazel(row,cwd,["query",*opts,"--output=label",pat+"*"])
        labels.check_returncode()
        names={l.split(":",1)[1] for l in labels.stdout.splitlines() if l.startswith(pat)}
        assert "license" in names and "gazelle_check" in names
        row["declares_status_file"]="gazelle.status" in names
        keep={"license","gazelle_check","gazelle.status"}
        drop=["-"+pat+n for n in sorted(names-keep)]
        args=["build","--platforms=//tools/bazeldefs:linux_amd64","--nobuild","--",pat+"*",*drop]
    events=BUNDLE.parent/("gazelle-"+name+".bep.tmp")
    args.insert(1,"--build_event_json_file="+str(events))
    result=bazel(row,cwd,args)
    configured=[]
    if events.is_file():
        for line in events.read_text().splitlines():
            e=json.loads(line)
            c=e.get("id",{}).get("targetConfigured",{})
            if c and "configured" in e:configured.append(c["label"])
        events.unlink()
    row["selected"]=[l for l in configured if l.rpartition(":")[-1] in ("license","gazelle_check","gazelle.status")]
    row["exit"]=result.returncode
    if external:
        row["outcome"]="ANALYZED" if not result.returncode else "UNAVAILABLE"
    else:
        row["status"]="PASS" if result.returncode==0 and "license" in [l.rpartition(":")[-1] for l in row["selected"]] and ("gazelle.status" in [l.rpartition(":")[-1] for l in row["selected"]])==(sha!=PARENT) else "FAIL"
    save()
def attempt(name,fn,*args):
    try:
        fn(name,*args)
    except Exception as e:
        rows.setdefault(name,{})["error"]=type(e).__name__+": "+str(e)[:400]
        rows[name]["status"]="ERROR"
        save()
save()
with tempfile.TemporaryDirectory(dir=os.environ["RUNNER_TEMP"]) as d:
    temp=Path(d)
    sentinel=temp/"generated-public-fixture"
    sentinel.write_text("Gazelle qualification fixture; safe to disclose.\n")
    record["outside_digest"]=hashlib.sha256(sentinel.read_bytes()).hexdigest()
    ordinary,outside,inside=[SOURCE/"pkg/bits"/n for n in NAMES]
    assert not any(p.exists() or p.is_symlink() for p in (ordinary,outside,inside))
    ordinary.write_text("Checked-in package ordinary file control.\n")
    outside.symlink_to(sentinel)
    inside.symlink_to(ordinary.name)
    try:
        attempt("old_inputs",inputs,OLD)
        attempt("fixed_inputs",inputs,PRODUCT)
    finally:
        for p in (outside,inside,ordinary):p.unlink()
    # New repositories use the same published gVisor source with its original
    # go_deps overrides and no edits to work around the supplier restriction.
    consumer=temp/"consumer"
    consumer.mkdir()
    version,=re.findall(r'bazel_dep\(name = "platforms", version = "([^"]+)"', (SOURCE/"MODULE.bazel").read_text())
    (consumer/"MODULE.bazel").write_text('module(name="gazelle_consumer")\nbazel_dep(name="gvisor",version="0.0.1")\nbazel_dep(name="platforms",version="%s")\n'%version)
    (consumer/"BUILD.bazel").touch()
    (consumer/".bazelversion").write_bytes((SOURCE/".bazelversion").read_bytes())
    for tag,sha in (("parent",PARENT),("fixed",PRODUCT)):
        attempt(tag+"_consumer",wildcard,sha,consumer,True)
        attempt(tag+"_root",wildcard,sha,SOURCE,False)
print(json.dumps(rows,indent=2),flush=True)
verified=all(rows.get(name,{}).get("status")=="PASS" for name in ("old_inputs","fixed_inputs","parent_root","fixed_root"))
verdict="INCOMPLETE"
if verified and all("exit" in rows.get(k,{}) for k in ("parent_consumer","fixed_consumer")):
    before,after=(rows[k]["exit"] for k in ("parent_consumer","fixed_consumer"))
    if before:
        blocked = all(any("go_deps.gazelle_override" in line and "is forbidden" in line for item in rows[k]["commands"] for line in item["errors"]) for k in ("parent_consumer","fixed_consumer"))
        verdict = "INHERITED_NON_ROOT_GAZELLE_OVERRIDE" if blocked else "EXTERNAL_BASELINE_FAILURE_SEE_ORIGINAL_ERRORS"
    else:
        verdict="EXTERNAL_REGRESSION" if after else "EXTERNAL_BOTH_ANALYZED"
record["wildcard_comparison"]=verdict
record["completed"]=now()
save()
raise SystemExit(0 if verified and verdict!="INCOMPLETE" else 1)
