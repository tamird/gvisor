"""Compare real Bazel wildcard selection with the root's development tools absent."""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

source = Path.cwd()
output = Path(os.environ["RUNNER_TEMP"]) / "shared-lint-wildcards"
output.mkdir()
rows = {}
errors = []
tools = {"buildifier": "BuildifierCheck", "cpplint": "Cpplint"}
sources = {key: os.environ[key] for key in ("BASE", "OLD", "FIXED")}

def now():
    return datetime.now(timezone.utc).isoformat()

def git(*args):
    return subprocess.check_output(["git", *args], cwd=source, text=True).strip()

def run(key, cwd, cmd):
    start = now()
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    rows[key] = {
        "command": cmd, "source": git("rev-parse", "HEAD"), "exit": p.returncode,
        "start": start, "finished": now(), "stdout_sha256": hashlib.sha256(p.stdout.encode()).hexdigest(),
        "error": [line for line in p.stderr.splitlines() if line.startswith("ERROR:") or "is forbidden" in line][-8:],
    }
    for ext, data in [("stdout",p.stdout),("stderr",p.stderr)]:
        # aquery emits the selected native action's full input set. Keep the
        # action key, owner and digest in the receipt without packaging the entire graph.
        if ext == "stdout" and "aquery" in cmd: continue
        (output/f"{key}.{ext}").write_text(data)
    print(key, p.returncode, flush=True)
    return p

def bsp(key,root,verb,*args):
    return run(key,root,["bazel","--ignore_all_rc_files",verb,"--ignore_dev_dependency","--override_module=gvisor="+str(source),*args])

def build(key,root,*patterns):
    event_path = root/(key+".bep")
    p = bsp(key,root,"build","--nobuild","--platforms=@platforms//host","--build_event_json_file="+str(event_path),"--",*patterns)
    events = [json.loads(x) for x in event_path.read_text().splitlines()] if event_path.exists() else []
    labels = sorted({e["id"]["targetConfigured"]["label"] for e in events if "targetConfigured" in e.get("id",{})})
    expanded = sorted({ch["targetConfigured"]["label"] for e in events if "expanded" in e for ch in e.get("children", []) if "targetConfigured" in ch})
    rows[key]["configured"] = labels
    rows[key]["expanded"] = expanded
    rows[key]["analysis_summary"] = [l.strip() for l in p.stderr.splitlines() if "Analyzed " in l]
    return p.returncode

def external(root,mode):
    for arm,sha in sources.items():
        git("checkout","--detach","--quiet",sha)
        module=source/"MODULE.bazel"
        original=module.read_text()
        key=mode+"-"+arm.lower()
        try:
            if mode == "gazelle-isolated":
                # Same temporary diagnostic used by the prior wildcard test:
                # as a dependency Gazelle rejects these ten root-only overrides.
                assert original.count("go_deps.gazelle_override(")==9 and original.count("go_deps.module_override(")==1
                name='root_go_deps = use_extension("@bazel_gazelle//:extensions.bzl", "go_deps", dev_dependency = True)\n'
                diagnostic=original.replace("go_deps.gazelle_override(","root_go_deps.gazelle_override(").replace("go_deps.module_override(",name+"root_go_deps.module_override(",1)
                module.write_text(diagnostic)
            for kind, pattern in [("rules", "@gvisor//:all"), ("files", "@gvisor//:*")]:
                pattern_key=key+"-"+kind
                q=bsp(pattern_key+"-names",root,"query",pattern)
                if q.returncode: continue
                labels={s.rsplit(":",1)[-1] for s in q.stdout.splitlines() if re.fullmatch(r"@{1,2}gvisor[+~\w.-]*//:[^\s]+",s)}
                rows[pattern_key+"-names"]["lint_aliases"]=sorted(labels & tools.keys())
                if "license" not in labels or (labels & tools.keys()) != (set() if arm=="BASE" else set(tools)):
                    errors.append(pattern_key+": incomplete root query");continue
                # The client builds the wildcard itself. Exclude unrelated old
                # build/test targets so their failures can't mask either alias.
                for alias in tools if arm!="BASE" else ["buildifier"]:
                    drop=["-@gvisor//:"+s for s in sorted(labels-{"license",alias})]
                    build(pattern_key+"-"+alias,root,pattern,*drop)
            if arm=="BASE":
                build(key+"-explicit-license",root,"@gvisor//:license")
        finally:
            if module.read_text()!=original: module.write_text(original)
            assert not git("diff","--name-only","HEAD")

def matches_pattern(mode,kind):
    baseline=rows.get(mode+"-base-"+kind+"-buildifier",{})
    explicit=rows.get(mode+"-base-explicit-license",{})
    if baseline.get("exit")!=0 or explicit.get("exit")!=0: return False
    for alias in tools:
        old=rows.get(mode+"-old-"+kind+"-"+alias,{})
        new=rows.get(mode+"-fixed-"+kind+"-"+alias,{})
        lint_error="\n".join(old.get("error",[]))
        if old.get("exit",0)==0 or not any(s in lint_error for s in ("lint_inputs","buildifier_prebuilt","cpplint")) or not any(s in lint_error.lower() for s in ("no repository visible","unknown repo","not visible","does not exist","could not find","no such repository","not declared")): return False
        selected = set(new.get("configured",[])) | set(new.get("expanded",[]))
        found_license = any(x.endswith("//:license") for x in selected) or any(re.search(r"Analyzed target @+gvisor[^ /]*//:license(?: |\.)",s) for s in new.get("analysis_summary",[]))
        if new.get("exit")!=0 or not found_license or any(x.endswith("//:"+a) for a in tools for x in selected): return False
    return True

def matches(mode):
    return all(matches_pattern(mode,kind) for kind in ("rules","files"))

try:
    start=now()
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp)
        pversion=re.search(r'bazel_dep\(name = "platforms", version = "([^"]+)"',git("show",sources["BASE"]+":MODULE.bazel")).group(1)
        (root/"MODULE.bazel").write_text(f'module(name="lint_alias_consumer")\nbazel_dep(name="gvisor",version="0.0.1")\nbazel_dep(name="platforms",version="{pversion}")\n')
        (root/"BUILD.bazel").touch()
        (root/".bazelversion").write_bytes((source/".bazelversion").read_bytes())
        external(root,"published")
        mode="published"
        if not matches(mode):
            mode="gazelle-isolated";external(root,mode)
        if not matches(mode):errors.append("did not establish complementary alias selection in either consumer")
    git("checkout","--detach","--quiet",sources["FIXED"])
    for alias,mnemonic in tools.items():
        key="root-explicit-"+alias
        cmd=["bazel","aquery","--config=x86_64","--output=jsonproto","--include_artifacts=false","--include_commandline=false",f'mnemonic("{mnemonic}", deps(//:{alias}))']
        p=run(key,source,cmd)
        if p.returncode: errors.append(key+": action query failed"); continue
        graph=json.loads(p.stdout)
        labels={t["id"]:t["label"] for t in graph.get("targets",[])}
        found=[{"owner": labels[a["targetId"]],"mnemonic":a["mnemonic"],"action_key":a.get("actionKey")} for a in graph.get("actions",[]) if a["mnemonic"]==mnemonic]
        rows[key]["actions"]=found
        if len(found)!=1 or not found[0]["owner"].endswith("//tools/lint:"+alias): errors.append(key+": selected wrong action")
except Exception as e:
    import traceback
    errors.append(traceback.format_exc())
finally:
    report={"ok":not errors,"sources":sources,"rows":rows,"errors":errors,"started":start,"finished":now()}
    (output/"verdict.json").write_text(json.dumps(report,indent=2)+"\n")
    print(errors,flush=True)
sys.exit(bool(errors))
