"""Check the upstream build_test adapter using the pinned stock Linux client."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import urllib.parse

WORK = Path(os.environ["RUNNER_TEMP"]) / "master-build-test"
ROOT = "//:build_test"
RUNNER = "//tools/bazeldefs:build_test_runner"
PKG = "//test/build_test_qualification:"
SOURCE = "e96adab0a6448c8e257b092a263f931f8e439193"
report = {"source": SOURCE, "graphs": {}, "verified": False}


def save():
    (WORK / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def bazel(command, args, name, config="x86_64"):
    flags = ["--jobs=4", "--config=" + config, "--lockfile_mode=off", "--spawn_strategy=processwrapper-sandbox",
             "--remote_cache=", "--remote_executor=", "--bes_backend=", "--color=no", "--curses=no", "--noshow_progress"]
    cp = subprocess.run([WORK/"bazel", "--nosystem_rc", "--nohome_rc", command, *flags, *args], capture_output=True)
    report.setdefault("commands", {})[name] = {
        "exit": cp.returncode, "stderr_sha256": sha(cp.stderr),
        "errors": [s.replace(str(Path.home()), "~")[:800] for s in cp.stderr.decode(errors="replace").splitlines()
                   if s.startswith(("ERROR:", "ld.lld: error:", "FATAL:", "race requires", "cannot use"))][:8]}
    save()
    return cp


def graph(config):
    names = [ROOT, RUNNER, PKG + "direct_good", PKG + "suite_good"]
    expr = "labels(targets, //:build_test) + set(" + " ".join(names) + ")"
    fmt = '[str(target.label), [f.path for f in target.files.to_list()], [f.path for f in providers(target)["DefaultInfo"].default_runfiles.files.to_list()] if str(target.label).endswith("//:build_test") else []]'
    cp = bazel("cquery", ["--output=starlark", "--starlark:expr=" + fmt, expr], "graph-" + config, config)
    rows = {}
    if cp.returncode == 0:
        for line in cp.stdout.decode().splitlines():
            label, files, runfiles = ast.literal_eval(line)
            rows[label.removeprefix("@@")] = {"files": files, "runfiles": runfiles}
    declared = re.findall(r'"(//test/[^"]+)"', Path("BUILD").read_text().split('name = "build_test",')[1].split("],")[0])
    deps = {f for n in declared for f in rows.get(n, {}).get("files", [])}
    root = rows.get(ROOT, {})
    outputs = set(root.get("files", []))
    own = outputs - deps
    good = (len(deps) == len(set(declared)) == 16 and len(outputs)==17 and deps <= outputs and len(own)==1
            and Path(next(iter(own))).name=="build_test" and set(root["runfiles"])==own | set(rows[RUNNER]["files"]))
    report["graphs"][config] = {"verified": good, "rows": rows}
    save()
    return rows


def formatting():
    paths = ["tools/bazeldefs/BUILD","tools/bazeldefs/defs.bzl"]
    warnings = re.search(r'local -r warnings="([^"]+)"', Path("tools/lint.sh").read_text())[1]
    p = subprocess.run([WORK/"buildifier","--mode=check","--lint=warn","--warnings="+warnings,*paths], capture_output=True)
    go = Path("tools/bazeldefs/build_test_runner.go").resolve()
    f = bazel("run", ["--run_in_cwd","@org_golang_x_tools//cmd/goimports","--","-format-only","-d",str(go)], "goimports")
    report["formatting"] = {"buildifier_version":"8.5.1", "buildifier_exit":p.returncode, "goimports_exit":f.returncode,
        "buildifier_diagnostic":(p.stdout+p.stderr).decode(errors="replace")[:3000], "go_diff":f.stdout.decode(errors="replace")[:2000],
        "verified":p.returncode==0 and f.returncode==0 and not f.stdout.strip()}
    save()


def elf(path):
    b=path.read_bytes()
    if b[:6]!=b"\x7fELF\x02\x01":
        return {}
    offset=struct.unpack_from("<Q",b,32)[0]
    stride,count=struct.unpack_from("<HH",b,54)
    return {"sha256":sha(b),"bytes":len(b),"machine":struct.unpack_from("<H",b,18)[0],
            "interpreter":any(struct.unpack_from("<I",b,offset+i*stride)[0]==3 for i in range(count))}


def read_events(path):
    actions, tests, sizes = [], {}, {}
    if not path.is_file():
        return actions,tests,sizes
    for line in path.open():
        event = json.loads(line)
        ident = event["id"]
        if "targetConfigured" in ident and "configured" in event and event["configured"].get("testSize"):
            sizes[ident["targetConfigured"]["label"].removeprefix("@@")] = event["configured"]["testSize"]
        if "actionCompleted" in ident and "action" in event:
            action=event["action"]
            label=ident["actionCompleted"].get("label","").removeprefix("@@")
            if (label==RUNNER and action.get("type")=="GoLink") or (label.startswith(PKG) and action.get("type")=="Genrule"):
                actions.append([label,action["type"],bool(action.get("success")),ident["actionCompleted"].get("primaryOutput")])
        if "testResult" not in ident or "testResult" not in event:
            continue
        test=event["testResult"]
        label=ident["testResult"]["label"].removeprefix("@@")
        row={"status":test["status"],"local_cache":bool(test.get("cachedLocally")),"remote_cache":bool(test.get("executionInfo",{}).get("cachedRemotely"))}
        for f in test.get("testActionOutput", []):
            if f["name"] not in ("test.log","test.xml"):
                continue
            uri=urllib.parse.urlsplit(f.get("uri",""))
            path=Path(urllib.parse.unquote(uri.path))
            if uri.scheme=="file" and path.as_posix().endswith("/testlogs/test/build_test_qualification/"+label.split(":")[-1]+"/"+f["name"]) and path.is_file():
                h=sha(path.read_bytes())
                assert not f.get("digest") or f["digest"]==h
                row[f["name"]]=h
                cfg=re.search(r"/bazel-out/([^/]+)/testlogs/", path.as_posix())
                if cfg:
                    row["output_config"]=cfg[1]
        tests[label]=row
    return actions,tests,sizes


def read_spawns(path):
    if not path.exists():
        return [],[]
    with subprocess.Popen([WORK/"bb","print","--raw","--compact_execution_log="+str(path)],stdout=subprocess.PIPE) as p:
        projection = 'select(has("spawn"))|.spawn|select((.targetLabel|startswith("//test/build_test_qualification:")) or (.targetLabel=="//tools/bazeldefs:build_test_runner" and .mnemonic=="GoLink"))|{label:.targetLabel,kind:.mnemonic,exe:.args[0],child:(if .mnemonic=="TestRunner" then .args[1] else null end),runner:.runner,cache:.cacheHit,exit:.exitCode}'
        scan=subprocess.run(["jq","-c",projection],stdin=p.stdout,capture_output=True)
        p.stdout.close()
        return [json.loads(line) for line in scan.stdout.splitlines()], [p.wait(),scan.returncode]


def physical(rows, race=False):
    name="race" if race else "ordinary"
    work=WORK/name
    work.mkdir()
    good={PKG+"direct_good"} if race else {PKG+"direct_good",PKG+"suite_good"}
    bad=set() if race else {PKG+"direct_broken",PKG+"suite_broken"}
    selected=[PKG+"direct_good"] if race else [PKG+"direct_good",PKG+"direct_broken",PKG+"selected"]
    options=["--keep_going","--nocache_test_results","--build_event_publish_all_actions",
             "--nobuild_event_json_file_path_conversion","--build_event_json_file="+str(work/"events"),"--execution_log_compact_file="+str(work/"compact")]
    if race:
        options.append("--config=race")
    cp=bazel("test",[*options,*selected],name)
    actions,tests,sizes=read_events(work/"events")
    spawns,decoder=read_spawns(work/"compact")
    root=subprocess.run([WORK/"bazel","--nosystem_rc","--nohome_rc","info","execution_root"],capture_output=True)
    native={}
    if root.returncode==0:
        execroot=Path(root.stdout.decode().strip())
        helper=rows.get(RUNNER,{}).get("files",[])
        for label in sorted(good):
            cfg=tests.get(label,{}).get("output_config")
            paths=[execroot/"bazel-out"/cfg/"bin"/"test/build_test_qualification"/label.split(":")[-1]] if cfg else []
            if not race:
                paths.extend(execroot/f for f in rows.get(label,{}).get("files",[]) if Path(f).name==label.split(":")[-1])
            path=next((p for p in paths if p.is_file()),None)
            if path:
                native[label]=elf(path)
                resolved=path.resolve()
                if race and cfg and path.is_symlink() and resolved.name=="build_test_runner":
                    native[RUNNER]=elf(resolved)
                    native[RUNNER]["from_test_config"]=cfg
                    native[RUNNER]["resolved_path"]=resolved.as_posix().split("/bazel-out/")[-1]
        if not race and RUNNER not in native and len(helper)==1 and (execroot/helper[0]).is_file():
            native[RUNNER]=elf(execroot/helper[0])
    def ran(owner,kind,code):
        return [s for s in spawns if s["label"]==owner and s["kind"]==kind and int(s["exit"] or 0)==code and not s["cache"]
                and s["runner"] in ("processwrapper-sandbox","linux-sandbox","local")]
    execution=(set(tests)==good and all(t["status"]=="PASSED" and not t["local_cache"] and not t["remote_cache"] and t.get("test.log") for t in tests.values())
               and all(any(Path(s["child"] or "").name==l.split(":")[-1] for s in ran(l,"TestRunner",0)) for l in good)
               and not any(s["kind"]=="TestRunner" and s["label"] in bad for s in spawns))
    link=(len(native)==1+len(good) and all(x.get("machine")==62 and not x["interpreter"] for x in native.values())
          and len({x["sha256"] for x in native.values()})==1 and
          (race or (ran(RUNNER,"GoLink",0) and any(a[:3]==[RUNNER,"GoLink",True] for a in actions))))
    inputs=(race or (all(ran(l+"_input","Genrule",0) for l in good) and all(ran(l+"_input","Genrule",41) for l in bad)))
    verified=bool(cp.returncode==(0 if race else 1) and decoder==[0,0] and execution and link and inputs)
    report[name]={"verified":verified,"original_exit":cp.returncode,"actions":actions,"spawns":spawns,"tests":tests,"executables":native,"sizes":sizes,"decoder":decoder}
    save()


save()
rows=graph("x86_64")
graph("aarch64")
size_query='attr(size, "^small$", set(//:build_test '+PKG+'direct_good)) + attr(size, "^medium$", '+PKG+'suite_good)'
sizes=subprocess.run([WORK/"bazel","--nosystem_rc","--nohome_rc","query","--output=label",size_query],capture_output=True)
report["sizes"]={"exit":sizes.returncode,"labels":sorted(sizes.stdout.decode().splitlines())}
report["sizes"]["verified"]=sizes.returncode==0 and set(report["sizes"]["labels"])=={ROOT,PKG+"direct_good",PKG+"suite_good"}
save()
physical(rows)
physical(rows,race=True)
formatting()
report["verified"]=all(report[key]["verified"] for key in ("ordinary","race","formatting","sizes")) and all(x["verified"] for x in report["graphs"].values())
save()
if not report["verified"]:
    raise SystemExit("Check the original compiler, TestRunner, graph and formatter receipts")
