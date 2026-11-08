"""Run the original and native language tests on a local Linux Bazel worker."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import unquote, urlsplit

BASE = "7d1b049f5d541992b47cc38cf564025908a9ed7b"
FIX = "03aa2130e511fa58b81a6312b19d012e054e8bd9"
IMG_SHA = "sha256:207921e49c91b81de35ec3ac8d82839c0a7566a9650f2c4584929aa1e946a540"
IMG_ID = "sha256:1ca51814ff949ca586d79fd0d1677a21919c16a560915e30f94cc58116d95410"
REMOTE = "us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/runtimes/python3.12.3_x86_64@" + IMG_SHA
LOCAL = "gvisor.dev/images/runtimes/python3.12.3:latest"
RUNTIME = "runsc-language-qualification"
WORK = Path(os.environ["RUNNER_TEMP"]) / "language-runner"
BAZEL = [str(WORK/"bazel"),"--nosystem_rc","--nohome_rc","--host_jvm_args=-Xmx4g"]
FLAGS = ["--config=x86_64","-c","opt","--jobs=4","--lockfile_mode=off","--remote_cache=","--remote_executor=",
         "--bes_backend=","--spawn_strategy=linux-sandbox","--strategy=GoStdlib=processwrapper-sandbox",
         "--strategy=GoStdlibList=processwrapper-sandbox","--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox"]
SUITES = {"go1.22":("go",50,"go1.22.csv"), "java21":("java",100,"java21.csv"),
          "nodejs22.2.0":("nodejs",50,"nodejs22.2.0.csv"), "php8.5.11":("php",50,None),
          "python3.12.3":("python",50,"python.csv")}
PKG = "//test/runtimes:"
PYTHON = PKG + "python3.12.3"
RELEASE = {"runsc","containerd-shim-runsc-v1"} | {"gvisor-bin/" + n for n in
           ["gvisor_sentry","checkpointgofer","runsc-metric-server","runsc-netgofer","runsc-fd-parking","gvisor-sentry-prewarmer","gvisor-proc-umounter"]}
RESULT = {"base":BASE,"fix":FIX,"carrier":os.environ["GITHUB_SHA"],"cells":{}}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save():
    (WORK/"result.json").write_text(json.dumps(RESULT,indent=2,sort_keys=True) + "\n")


def output(args):
    return subprocess.check_output(args,text=True).strip()


def cell(name,fn,*args):
    row = RESULT["cells"][name] = {"verified":False}
    save()
    try:
        fn(name,row,*args)
        row["verified"] = True
    except Exception as err:
        row["error"] = f"{type(err).__name__}: {err}"[:450]
    save()


def diagnostics(path):
    lines=path.read_text(errors="replace").splitlines()
    ix=[i for i,s in enumerate(lines) if re.search(r"ERROR:|FATAL:|error:|No space left",s)][:3]
    indexes=sorted({j for i in ix for j in range(i,min(i+5,len(lines)))} | set(range(max(0,len(lines)-5),len(lines))))
    return [re.sub(r"https?://\S+","<url>",lines[i])[:460] for i in indexes]


def run_tests(name,row,owners,args,masked=False,probe=True):
    bep=WORK/(name+".bep")
    log=WORK/(name+".log")
    cmd=[*BAZEL,"test",*FLAGS,"--strategy=TestRunner=local","--test_sharding_strategy=disabled",
         "--nocache_test_results","--flaky_test_attempts=1","--keep_going","--build_event_json_file="+str(bep)]
    if probe:
        cmd+=["--run_under="+str(WORK/"probe.py"),"--test_env=LANGUAGE_MASK="+str(int(masked))]
    cmd += ["--test_arg="+arg for arg in args] + owners
    with log.open("w") as stream:
        row["bazel_exit"]=subprocess.run(cmd,stdout=stream,stderr=subprocess.STDOUT).returncode
    if row["bazel_exit"]:
        row["diagnostics"]=diagnostics(log)
    row["tests"]={}
    save()
    for line in bep.open():
        event=json.loads(line)
        key=event["id"].get("testResult",{})
        if "started" in event:
            row["invocation"]=event["started"]["uuid"]
        if not key or "testResult" not in event:
            continue
        label=key["label"]
        assert label in owners and label not in row["tests"]
        result=event["testResult"]
        info=result.get("executionInfo",{})
        item,=[i for i in result["testActionOutput"] if i["name"]=="test.log"]
        url=urlsplit(item["uri"])
        assert url.scheme=="file" and not url.netloc
        path=Path(unquote(url.path))
        data=path.read_bytes()
        checksum=hashlib.sha256(data).hexdigest()
        assert item.get("digest",checksum)==checksum and int(item.get("length",len(data)))==len(data)
        lines=data.decode(errors="replace").splitlines()
        observed=row["tests"][label]={"status":result["status"],"cached":bool(result.get("cachedLocally") or info.get("cachedRemotely")),
            "attempt":key.get("attempt",1),"strategy":info.get("strategy"),"log_sha256":checksum,
            "marker":[json.loads(s.removeprefix("Q_LANGUAGE ")) for s in lines if s.startswith("Q_LANGUAGE ")],
            "errors":[s[:450] for s in lines if s.startswith("Error getting exclude list:") or "docker run failed:" in s or "FAIL:" in s][-6:],
            "usage":any("Usage of" in s for s in lines), "batch":[s[-420:] for s in lines if "Running test case batch:" in s or re.search(r"PASS: .*\d+ tests passed",s)],
            "copy":[s[-500:] for s in lines if "copy:" in s and "proctor_bin" in s][-2:]}
        save()
        assert not observed["cached"] and observed["attempt"]==1 and observed["strategy"] in ["local","standalone"]
    assert set(row["tests"])==set(owners),row["tests"].keys()
    return row["tests"]


def refs(name,row):
    labels=["//test/runtimes/runner:runner","//test/runtimes/proctor:proctor_bin","//:release"]
    text=output([*BAZEL,"cquery",*FLAGS,"--output=files","set("+" ".join(labels)+")"])
    paths=[Path(p) for p in text.splitlines()]
    row["runner"],=[digest(p) for p in paths if p.name=="runner"]
    row["proctor"],=[digest(p) for p in paths if p.name=="proctor_bin"]
    row["release"]={p.as_posix().split("/release/",1)[1]:digest(p) for p in paths if "/release/" in p.as_posix()}
    assert set(row["release"])==RELEASE,row["release"].keys()
    runsc, = [p for p in paths if p.as_posix().endswith("/release/runsc")]
    row["runsc_version"] = output([str(runsc),"--version"]).splitlines()[0]
    row["git_version"] = output(["git","describe","--always","--tags","--exclude","release-*-staging","--abbrev=12","--dirty"])
    row["version_matches"] = row["git_version"] in row["runsc_version"]



def controls(name,row,phase,case):
    bad=WORK/"exclusion file.csv"
    owners=[PKG+k for k in SUITES] if case in ["help","masked"] else [PYTHON]
    args=["--help"] if case in ["help","masked"] else ["--exclude_file="+str(bad)]
    tests=run_tests(name,row,owners,args,masked=case=="masked")
    fail=case=="spaces" or case=="masked" and phase=="base"
    assert row["bazel_exit"] == (3 if fail else 0)
    for label,t in tests.items():
        mark,=t["marker"]
        assert t["status"] == ("FAILED" if fail else "PASSED")
        assert set(mark["release"])==RELEASE and mark["proctor"][1]==RESULT["cells"][phase+"-files"]["proctor"]
        assert mark["release"]==RESULT["cells"][phase+"-files"]["release"]
        if phase=="fixed":
            assert mark["format"]=="ELF" and mark["entry_sha256"]==RESULT["cells"][phase+"-files"]["runner"]
        else:
            assert mark["format"]=="#!/bin/bash"
        if case=="masked" and phase=="base":
            assert mark["errno"]==2 and mark["exit"]==127
            continue
        argv,=mark["go_argv"]
        suite=label.split(":")[-1]
        lang,batch,csv=SUITES[suite]
        flags={"batch":"50"}
        fixed_count=1+(csv is not None)+(phase=="base" or batch==100)+1
        for i in range(0,2*fixed_count,2):
            flags[argv[i].lstrip("-")]=argv[i+1]
        assert flags=={"image":suite,"lang":lang,"batch":str(batch),**({"exclude_file":"test/runtimes/exclude/"+csv} if csv else {})},flags
        if case == "spaces":
            if phase == "base":
                assert argv[2*fixed_count:] == ["--exclude_file="+str(bad).split(" ")[0], "file.csv"]
                assert any("no such file or directory" in s and str(bad).split(" ")[0] in s for s in t["errors"])
            else:
                assert argv[2*fixed_count:] == args and any("wrong number of fields" in s for s in t["errors"])
        else:
            assert argv[2*fixed_count:] == ["--help"] and t["usage"] and mark["exit"] == 0


def spaced_label(name, row):
    label = PKG+"input_path"
    filename = "exclusion path's $(literal).csv"
    directory = Path("test/runtimes")
    (directory / filename).write_text("name,reason,comment\njust-one-field\n")
    with (directory / "BUILD").open("a") as stream:
        stream.write('''
config_setting(name="language_csv",define_values={"language_csv":"1"})
filegroup(name="chosen_csv",srcs=select({":language_csv":["exclusion path's $(literal).csv"],"//conditions:default":["exclude/python.csv"]}))
runtime_test(name="input_path",args=["--lang","python","--exclude_file","$(rootpath :chosen_csv)"],data=[":chosen_csv"],shard_count=1)
''')
    FLAGS.append("--define=language_csv=1")
    try:
        observed = run_tests(name, row, [label], [], masked=True)[label]
        marker, = observed["marker"]
        assert marker["go_argv"] == [["--image","input_path","--lang","python","--exclude_file","test/runtimes/"+filename]]
        assert row["bazel_exit"] == 3 and marker["exit"] == 1 and any("wrong number of fields" in s for s in observed["errors"])
    finally:
        FLAGS.pop()
        subprocess.run(["git","restore","--","test/runtimes/BUILD"],check=True)
        (directory / filename).unlink()


def recent_proctor(start, end):
    log = output(["docker","events","--since",start,"--until",end,"--filter","type=container","--format","{{json .}}"])
    events = [json.loads(line) for line in log.splitlines()]
    outcomes = []
    for event in events:
        action = event.get("Action", "")
        if not action.startswith("exec_create:") or "proctor_bin --runtime python --tests test_bool " not in action:
            continue
        actor = event["Actor"]
        exec_id = actor["Attributes"]["execID"]
        finish, = [e["Actor"]["Attributes"] for e in events if e.get("Action") == "exec_die" and
                   e["Actor"]["ID"] == actor["ID"] and e["Actor"]["Attributes"].get("execID") == exec_id]
        outcomes.append({"container": actor["ID"], "exec": exec_id, "command": action, "exit": finish["exitCode"],
                         "image": actor["Attributes"].get("image")})
    return outcomes


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")


def guest(name, row):
    row["image_manifest"] = IMG_SHA
    subprocess.run(["docker","pull",REMOTE],check=True)
    image_id = output(["docker","image","inspect",REMOTE,"--format={{.Id}}"])
    assert image_id == IMG_ID
    row["image_config"] = image_id
    subprocess.run(["docker","tag",REMOTE,LOCAL],check=True)
    dest = WORK/"release"
    shutil.copytree("bazel-bin/release",dest)
    assert {str(p.relative_to(dest)):digest(p) for p in dest.rglob("*") if p.is_file()} == RESULT["cells"]["fixed-files"]["release"]
    subprocess.run(["sudo","-n",str(dest/"runsc"),"install","--experimental=true","--runtime="+RUNTIME,"--","--platform=systrap","--directfs=true"],check=True)
    subprocess.run(["sudo","-n","systemctl","restart","docker"],check=True)
    runtime = json.loads(output(["docker","info","--format={{json .Runtimes}}"]))[RUNTIME]
    row["runtime"] = {"path":runtime.get("path"),"sha256":digest(dest/"runsc")}
    assert Path(runtime["path"]).samefile(dest/"runsc")
    save()
    start = now()
    good = WORK/"valid exclusion file.csv"
    shutil.copyfile("test/runtimes/exclude/python.csv",good)
    test = run_tests(name,row,[PYTHON],["--runtime="+RUNTIME,"--tests=test_bool","--exclude_file="+str(good),"--timeout=5m"],probe=False)[PYTHON]
    outcomes = row["original_proctor_exec"] = recent_proctor(start, now())
    save()
    assert row["bazel_exit"] == 0 and test["status"]=="PASSED" and any("Running test case batch: test_bool" in s for s in test["batch"])
    assert any(re.search(r"PASS: .* 1 tests passed",s) for s in test["batch"]), test
    actual, = outcomes
    assert actual["exit"]=="0" and actual["image"].removesuffix(":latest")==LOCAL.removesuffix(":latest"), actual
    assert test["copy"]
    # The stock runner only prints the proctor's stdout on failure. Repeat
    # this single case on the same proctor and image to retain CPython's result.
    p, = [p for p in Path("bazel-bin/test/runtimes/proctor").glob("**/proctor_bin") if p.is_file() and ".runfiles/" not in str(p)]
    assert digest(p) == RESULT["cells"]["fixed-files"]["proctor"]
    command = ["docker","run","--rm","--network=none","--runtime="+RUNTIME,
               "--mount","type=bind,src="+str(p.resolve())+",dst=/proctor/proctor_bin,readonly",
               "--entrypoint","/proctor/proctor_bin",LOCAL,"--runtime","python","--tests","test_bool","--runs_per_test","1","--timeout","5m"]
    proc = subprocess.run(command,capture_output=True,text=True,timeout=330)
    lines = (proc.stdout+"\n"+proc.stderr).splitlines()
    row["direct_proctor_replay"] = {"exit":proc.returncode,"image_config":image_id,"proctor_sha256":digest(p),
       "lines":[s[:400] for s in lines if re.search(r"test_bool|tests run|Tests result|tests? OK|Result:|Total tests",s,re.I)][-20:]}
    save()
    assert proc.returncode == 0 and any("test_bool" in s for s in lines)
    assert any(re.search(r"Total tests: run=[1-9]",s) for s in lines) and any("Tests result: SUCCESS" in s for s in lines), row["direct_proctor_replay"]


def format_source(name, row):
    lint = Path("tools/lint.sh").read_text()
    warnings, = re.findall(r'local -r warnings="([^"]*-native-proto)"', lint)
    binary = WORK / "buildifier"
    checksum = "887377fc64d23a850f4d18a077b5db05b19913f4b99b270d193f3c7334b5a9a7"
    assert digest(binary) == checksum and checksum in lint and 'BUILDIFIER_VERSION="8.5.1"' in lint
    files = ["test/runtimes/BUILD","test/runtimes/defs.bzl"]
    proc = subprocess.run([str(binary),"--mode=check","--lint=warn","--warnings="+warnings,*files],capture_output=True,text=True)
    row.update(binary_sha256=checksum, files=files, exit=proc.returncode, output=(proc.stdout+proc.stderr)[:2000])
    assert proc.returncode == 0, row["output"]


def main():
    assert output(["git","rev-parse","HEAD"]) == RESULT["carrier"] and output(["git","rev-parse","HEAD^"]) == FIX
    assert output(["git","rev-parse","HEAD^^"]) == BASE
    assert set(output(["git","diff","--name-only",BASE,FIX]).splitlines()) == {"test/runtimes/BUILD","test/runtimes/defs.bzl"}
    assert set(output(["git","diff","--name-only",FIX,"HEAD"]).splitlines()) == {
        ".github/qualification/language_runner/probe.py",".github/qualification/language_runner/check.py",".github/workflows/build.yml"}
    assert output([BAZEL[0],"--version"]) == "bazel 9.3.1"
    (WORK/"exclusion file.csv").write_text("name,reason,comment\njust-one-field\n")
    save()
    for phase,rev in [("base",BASE),("fixed",FIX)]:
        subprocess.run(["git","switch","--detach",rev],check=True)
        assert output(["git","rev-parse","HEAD"]) == rev
        if phase == "fixed":
            cell("buildifier",format_source)
        log = WORK/(phase+"-build.log")
        with log.open("w") as stream:
            status = subprocess.run([*BAZEL,"build",*FLAGS,*[PKG+k for k in SUITES],"//:release","//test/runtimes/runner:runner","//test/runtimes/proctor:proctor_bin"],stdout=stream,stderr=subprocess.STDOUT).returncode
        build = RESULT["cells"][phase+"-build"] = {"verified":status==0,"bazel_exit":status}
        if status:
            build["diagnostics"] = diagnostics(log)
        save()
        if status:
            continue
        cell(phase+"-files",refs)
        for case in ["help","masked","spaces"]:
            cell(phase+"-"+case,controls,phase,case)
    cell("declared-spaced-csv",spaced_label)
    cell("python-under-runsc",guest)
    RESULT["verified"] = all(c["verified"] for c in RESULT["cells"].values())
    save()
    assert RESULT["verified"]


if __name__ == "__main__":
    main()
