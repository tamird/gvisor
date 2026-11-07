"""Qualify the build_test inputs without running its integration or benchmark binaries."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

WORK = Path(os.environ["RUNNER_TEMP"], "mixed-build-test")
OUTPUT = WORK / "results"
BASE = "4b87bd4875ecde1ab6b994030d59871b59363ca1"
PRODUCT = "a5f8e93fb181bb1ea7eb055353ee4bbf8d8a61b1"
CPU = {"amd64": 62, "arm64": 183}
CONFIG = {"amd64": "source-presubmit", "arm64": "source-presubmit-arm64", "mixed": "source-presubmit-mixed"}
ONLY = "//:build_test"
LABEL = re.compile(r"//test/[\w/]+:[a-z0-9_]+_test")
SUITES = set((WORK / "suites.txt").read_text().splitlines())
FOCUS = {"build interrupted","no matching toolchains","no matching execution platform","not compatible",
         "resource exhausted","out of memory","signal: killed","timed out","missing input","conflicting actions",
         "failed to build","error executing","no registered executors","disk full","no space left"}
CLIENT = [str(WORK/"bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx8g"]
assert len(SUITES) == 16 and all(LABEL.fullmatch(l) for l in SUITES)


def save(name, obj):
    (OUTPUT / (name + ".json")).write_text(json.dumps(obj,sort_keys=True,indent=1)+"\n")


def source():
    return subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()


def call(key, kind, args, private=False):
    startup = [*CLIENT]
    if private:
        startup.append("--bazelrc="+str(WORK/"remote.rc"))
    command=[*startup,kind,*args,"--color=no","--curses=no","--noshow_progress"]
    with (WORK/(key+".out")).open("wb") as out,(WORK/(key+".log")).open("wb") as err:
        status=subprocess.run(command,stdout=out,stderr=err).returncode
    log=(WORK/(key+".log")).read_text(errors="replace").lower()
    return status,sorted(term for term in FOCUS if term in log)


def expected(mode, old):
    if mode=="mixed":
        return {"amd64"} if old else set(CPU)
    return {mode}


def configure(key, mode):
    sha=source()
    assert sha in (BASE,PRODUCT)
    star=WORK/"suite.star"
    star.write_text("wanted = "+repr(sorted(SUITES))+"\n"+'''def format(target):
    label = str(target.label).lstrip("@")
    if label not in wanted:
        return ""
    opts = build_options(target) or {}
    if opts.get("//command_line_option:is exec configuration", False):
        return ""
    return "\\t".join([label,str(opts.get("//command_line_option:platforms", "")), "|".join([f.path for f in target.files.to_list()])])
''')
    rc,diagnostics=call(key,"cquery",["--config="+CONFIG[mode],"--spawn_strategy=local","--remote_executor=",
        "--remote_cache=","--bes_backend=","--jobs=4","--output=starlark","--starlark:file="+str(star),"deps("+ONLY+")"])
    rows={}
    errors=[]
    for line in (WORK/(key+".out")).read_text().splitlines():
        if not line.strip():
            continue
        label,platform,files=line.split("\t")
        package,name=label[2:].split(":")
        arch=next((a for a in CPU if "linux_"+a in platform),None)
        paths=files.split("|")
        if label not in SUITES or not arch or len(paths)!=1 or not re.fullmatch(
                r"bazel-out/[\w.+-]+/bin/"+re.escape(package)+r"/(?:[\w.+-]+/)*"+re.escape(name),paths[0]):
            errors.append(label if label in SUITES else "unexpected label")
            continue
        if arch in rows.setdefault(label,{}) and rows[label][arch]!=paths[0]:
            errors.append(label)
        rows[label][arch]=paths[0]
    ok=rc==0 and not errors and rows.keys()==SUITES and all(set(v)==expected(mode,sha==BASE) for v in rows.values())
    paths=[p for v in rows.values() for p in v.values()]
    ok=ok and len(set(paths))==len(paths)
    record={"source":sha,"mode":mode,"exit":rc,"diagnostics":diagnostics,"rows":rows,"errors":errors,"verified":ok}
    save(key,record)
    if not ok:
        raise SystemExit("original target paths or CPU counts differ")


def execute():
    assert source()==PRODUCT
    records = {k:json.loads((OUTPUT/(k+".json")).read_text()) for k in ["base-mixed","new-amd64","new-arm64","new-mixed"]}
    assert all(d["verified"] for d in records.values())
    assert records["base-mixed"]["source"]==BASE and all(records[k]["source"]==PRODUCT for k in ("new-amd64","new-arm64","new-mixed"))
    paths=[p for v in records["new-mixed"]["rows"].values() for p in v.values()]
    assert len(set(paths))==32
    pattern_file=WORK/"root.targets"
    pattern_file.write_text(ONLY+"\n")
    rc,diagnostics=call("test","test",["--config=source-presubmit-mixed","--jobs=55","--noremote_local_fallback",
        "--nocache_test_results","--flaky_test_attempts=1","--modify_execution_info=^TestRunner$=+no-remote-cache",
        "--remote_download_outputs=minimal","--remote_download_regex=^("+"|".join(map(re.escape,paths))+")$",
        "--target_pattern_file="+str(pattern_file),"--build_event_json_file="+str(WORK/"test.bep")],private=True)
    save("execution",{"source":PRODUCT,"exit":rc,"diagnostics":diagnostics,"requested_binary_downloads":len(paths)})


def collect():
    assert source()==PRODUCT and not (WORK/"remote.rc").exists()
    execution=json.loads((OUTPUT/"execution.json").read_text())
    rows=json.loads((OUTPUT/"new-mixed.json").read_text())["rows"]
    rc,diagnostics=call("execroot","info",["execution_root"])
    assert rc==0
    execroot=Path((WORK/"execroot.out").read_text().strip())
    observations=[]
    for line in (WORK/"test.bep").open():
        e=json.loads(line)
        if "started" in e:
            execution["invocation"]=e["started"].get("uuid")
        if "testResult" in e:
            label=e["id"]["testResult"]["label"]
            t=e["testResult"]
            info=t.get("executionInfo",{})
            observations.append({"test":label if label==ONLY else "other","status":t["status"],
                "remote":info.get("strategy")=="remote","cached":bool(t.get("cachedLocally") or info.get("cachedRemotely"))})
    execution["test_results"]=observations
    actual=[]
    for label,arches in sorted(rows.items()):
        for arch,rel in sorted(arches.items()):
            p=execroot/rel
            entry={"label":label,"cpu":arch,"exec_path":rel,"present":p.is_file()}
            if p.is_file():
                h=hashlib.sha256()
                with p.open("rb") as stream:
                    header=stream.read(64)
                    h.update(header)
                    for chunk in iter(lambda:stream.read(1<<20),b""):
                        h.update(chunk)
                entry.update(size=p.stat().st_size,sha256=h.hexdigest(),mode=p.stat().st_mode&0o7777,
                    elf64_le=header[:6]==b"\x7fELF\x02\x01",machine=int.from_bytes(header[18:20],"little"))
            actual.append(entry)
    execution["binaries"]=actual
    execution["verified"]=(execution["exit"]==0 and observations==[{"test":ONLY,"status":"PASSED","remote":True,"cached":False}]
        and len(actual)==32 and all(r["present"] and r["elf64_le"] and r["machine"]==CPU[r["cpu"]] and r["size"]>64 for r in actual))
    save("products",execution)
    if not execution["verified"]:
        raise SystemExit("incomplete original test or installed ELF evidence")


if __name__=="__main__":
    if sys.argv[1]=="configure":
        configure(*sys.argv[2:])
    elif sys.argv[1]=="execute":
        execute()
    elif sys.argv[1]=="collect":
        collect()
    else:
        raise ValueError("unknown task")
