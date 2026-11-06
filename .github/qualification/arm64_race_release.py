"""Build the two public ARM64 race binaries on the released AMD64 worker."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import traceback
import uuid

from arm64_race_elf import elf


SOURCE = "83f2d8568e1799eab47b02dc92da2988bdb001d9"
BASE = "87fd1b411e0f2d06557f04e1f33b154692cfd2eb"
IMAGE = "docker://gcr.io/distroless/static-debian13@sha256:d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"
PLATFORM = "@llvm//platforms:linux_aarch64_glibc_static"
EXEC = "//test/rbe/release_prewarmer:amd64"
TMP = Path(os.environ["RUNNER_TEMP"]) / "release-rbe"
PAYLOAD = TMP / "handoff"
OUTPUTS = {
    "//runsc:runsc-race": "runsc",
    "//runsc/cmd/sentry:gvisor_sentry-race": "gvisor-bin/gvisor_sentry",
    "//runsc/prewarmer:_linked/gvisor-sentry-prewarmer": "gvisor-bin/gvisor-sentry-prewarmer",
    "//runsc/fdparking:_linked/runsc-fd-parking": "gvisor-bin/runsc-fd-parking",
    "//runsc/procumounter:_linked/gvisor-proc-umounter": "gvisor-bin/gvisor-proc-umounter",
}
RACE = list(OUTPUTS)[:2]


def record(name, data):
    (TMP / (name + ".json")).write_text(json.dumps(data, indent=2) + "\n")


def identity():
    return {"source": SOURCE, "base": BASE, "run": os.environ["GITHUB_RUN_ID"],
            "carrier": os.environ["GITHUB_SHA"], "status": "INCOMPLETE"}


def capture(command):
    return subprocess.check_output(command, text=True).strip()


def product():
    assert capture(["git", "rev-parse", "HEAD"]) == SOURCE
    assert capture(["git", "rev-parse", "HEAD^"]) == BASE
    assert capture(["git", "diff", "--name-only", BASE, SOURCE]) == "MODULE.bazel\ntools/llvm-static-glibc-unwind.patch"
    assert capture(["git", "rev-parse", "HEAD:tools/llvm-static-glibc-unwind.patch"]) == "fd0c6bb1478361caa32bd42c66f6551f5e70a8a5"
    assert capture(["git", "rev-parse", "HEAD:runsc/BUILD"]) == "27e9a762f62da298b6cfe5c6b52e76da0dbfa134"
    assert capture(["git", "rev-parse", "HEAD:runsc/cmd/sentry/BUILD"]) == "b5bef6d13d48ab79f5d1f83f439d90a2c5b2e52a"
    assert not capture(["git", "diff", "--name-only", "HEAD"])
    assert capture(["git", "ls-files", "--others", "--exclude-standard"]) == "test/rbe/release_prewarmer/BUILD"
    assert hashlib.sha256(Path("test/rbe/release_prewarmer/BUILD").read_bytes()).hexdigest() == "ae1f23a11f3cb747fbec49a34c510c5ab9dcb75d06d94bfd4d9a49d50c629271"


def events(path, report):
    links, complete, failures = {}, {}, []
    for line in path.open():
        e = json.loads(line)
        key = e["id"]
        if "started" in e:
            assert e["started"]["uuid"] == report["invocation"]
        if "finished" in e:
            report["bazel_exit"] = e["finished"]["exitCode"].get("code", 0)
        if k := key.get("targetCompleted"):
            if k["label"] in OUTPUTS and not k.get("aspect") and "completed" in e:
                complete[k["label"]] = [k["configuration"]["id"], e["completed"].get("success", False)]
        if a := e.get("action"):
            k = key["actionCompleted"]
            owner = k.get("label", "")
            if owner in OUTPUTS and a["type"] == ("GoLink" if owner in RACE else "CppLink"):
                links[owner] = [k["configuration"]["id"], k["primaryOutput"], a.get("success", False)]
            if not a.get("success") and len(failures) < 10:
                detail = a.get("failureDetail", {})
                failures.append({"owner":owner if re.fullmatch(r"[@A-Za-z0-9+_./:-]{1,220}", owner) else "<unknown>",
                                 "type":a["type"], "exit":a.get("exitCode"), "code":detail.get("spawn", {}).get("code")})
    report.update(links=links, completed=complete, failures=failures)
    return links, complete


def spawns(report):
    platform_properties = {"Arch":"amd64", "OSFamily":"linux", "workload-isolation-type":"oci",
                           "recycle-runner":"false", "container-image":IMAGE}
    query = """
      select(.spawn) | .spawn | . as $s |
      [.runner, (.cacheHit//false), (.exitCode//0),
       ([.platform.properties[]? | {key:.name,value:.value}] | from_entries | contains($platform)),
       (if ($race|index($s.targetLabel))!=null and .mnemonic=="GoLink" then .targetLabel else null end)]
    """
    counts, links = Counter(), {}
    with subprocess.Popen([TMP / "bb", "print", "--raw", "--compact_execution_log=" + str(TMP / "build.compact")],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as reader:
        with subprocess.Popen(["jq", "-c", "--argjson", "platform", json.dumps(platform_properties),
                               "--argjson", "race", json.dumps(RACE), query], stdin=reader.stdout,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
            reader.stdout.close()
            for line in decoder.stdout:
                runner, cached, code, matches, label = json.loads(line)
                counts[runner if runner in ("remote","remote cache hit") else "other"] += 1
                if not matches:
                    counts["different_platform"] += 1
                if label:
                    assert label not in links
                    links[label] = {"remote":runner=="remote", "cached":cached, "code":code, "platform_match":matches}
            assert decoder.wait() == 0
        assert reader.wait() == 0
    report.update(runners=dict(counts), race_spawns=links, worker=IMAGE)


def producer(report):
    assert platform.machine() == "x86_64"
    product()
    flags = ["--config=aarch64", "-c", "opt", "--config=race", "--platforms="+PLATFORM,
             "--extra_execution_platforms="+EXEC, "--host_platform="+EXEC,
             "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash",
             "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
             "--lockfile_mode=off", "--jobs=40", "--remote_download_outputs=toplevel",
             "--color=no", "--curses=no", "--noshow_progress"]
    bazel = [str(TMP / "bazel"), "--nosystem_rc", "--nohome_rc", "--bazelrc="+str(TMP/"private.bazelrc")]
    report.update(invocation=str(uuid.uuid4()), platform=PLATFORM)
    bep = TMP / "build.bep"
    record("producer", report)
    with (TMP / "build.log").open("wb") as log:
        rc = subprocess.run(["timeout","--kill-after=20","2400",*bazel,"build",*flags,
                             "--keep_going","--modify_execution_info=^GoLink$=+no-remote-cache",
                             "--invocation_id="+report["invocation"],"--build_event_publish_all_actions",
                             "--build_event_json_file="+str(bep),"--execution_log_compact_file="+str(TMP/"build.compact"),
                             *OUTPUTS], stdout=log,stderr=subprocess.STDOUT).returncode
    report["native_exit"] = rc
    links, complete = events(bep, report)
    if rc:
        data = (TMP/"build.log").read_bytes()
        report["observed_errors"] = sorted({m.decode() for m in re.findall(
            rb"(?:undefined symbol: [A-Za-z_.$][A-Za-z0-9_.$]{0,110}|unable to find library -l[A-Za-z0-9_+-]+|patch failed|No matching toolchains found|No matching execution platform|No space left on device|Resource Exhausted|Permission denied|No such file or directory)", data, re.I)})[:18]
    record("producer", report)
    if (TMP / "build.compact").is_file():
        spawns(report)
    record("producer", report)
    files = report["files"] = {}
    for owner, destination in OUTPUTS.items():
        if owner not in links or owner not in complete or not complete[owner][1]:
            continue
        cfg, output, ok = links[owner]
        assert ok and complete[owner] == [cfg, True]
        path = Path(output)
        assert not path.is_absolute() and path.parts[0]=="bazel-out" and ".." not in path.parts
        assert path.resolve(strict=True).is_relative_to(Path("bazel-out").resolve(strict=True))
        assert 0 < path.stat().st_size < 250 * 1024**2
        info = files[destination] = elf(path)
        assert info["machine"]==183 and info["static"]
        if owner in RACE:
            assert info["race_functions"] == ["runtime.racefini","runtime.raceinit"]
        out = PAYLOAD / destination
        out.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path, out)
        assert hashlib.sha256(out.read_bytes()).hexdigest() == info["sha256"]
        record("producer", report)
    assert rc == report["bazel_exit"] == 0 and set(files)==set(OUTPUTS.values())
    assert report["runners"].get("different_platform",0) == report["runners"].get("other",0) == 0
    assert set(report["race_spawns"]) == set(RACE) and all(
        s == {"remote":True,"cached":False,"code":0,"platform_match":True} for s in report["race_spawns"].values())
    report["status"] = "PASS"
    (PAYLOAD / "producer.json").write_text(json.dumps(report)+"\n")
    assert {str(p.relative_to(PAYLOAD)) for p in PAYLOAD.rglob("*") if p.is_file()} == {*OUTPUTS.values(),"producer.json"}


def consumer(report):
    import runtime_guest as g
    assert platform.machine()=="aarch64" and os.getuid() != 0
    original=json.loads((PAYLOAD/"producer.json").read_bytes())
    for k in ("source","base","run","carrier"):
        assert original[k] == report[k]
    assert original["status"]=="PASS" and set(original["files"])==set(OUTPUTS.values())
    observed={str(p.relative_to(PAYLOAD)) for p in PAYLOAD.rglob("*") if p.is_file()}
    assert observed == {*OUTPUTS.values(),"producer.json"}
    install = TMP/"install"
    for dest, expected in original["files"].items():
        path=PAYLOAD/dest
        assert not path.is_symlink() and elf(path)==expected
        out = install/dest
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path,out)
        out.chmod(0o755)
        assert hashlib.sha256(out.read_bytes()).hexdigest()==expected["sha256"]
    report["received"]={k:v["sha256"] for k,v in original["files"].items()}
    version=subprocess.run([install/"runsc","--version"], capture_output=True, timeout=50)
    report["version"]={"exit":version.returncode,"stamp":SOURCE[:9].encode() in version.stdout}
    record("consumer",report)
    assert report["version"]=={"exit":0,"stamp":True}
    g.r.SOURCE=SOURCE
    g.MEMBERS=set(OUTPUTS.values())
    busy=g.fixture(report.setdefault("fixture",{}),"arm64")
    control,_=g.capsule("namespace-control",busy)
    section=report["namespace_control"]={}
    g.launch(control,section,control=True)
    record("consumer",report)
    assert section["exit"]==0 and section["private_root_entered"]
    root,marker=g.capsule("guest-root",busy,install)
    smoke=report["smoke"]={}
    output=g.launch(root,smoke)
    smoke["marker"]=output.splitlines()==[marker.encode()]
    assert smoke["exit"]==0 and all(smoke[k] for k in ("private_root_entered","marker","sentry_selected","sentry_stdio_witness","sentry_release_matches"))
    report["status"]="PASS"


mode, = sys.argv[1:]
assert mode in ("producer", "consumer")
report = identity()
try:
    (producer if mode=="producer" else consumer)(report)
except (AssertionError, ValueError, KeyError, OSError, subprocess.SubprocessError) as err:
    report["status"]="FAIL"
    report["error"]={"type":type(err).__name__,"line":traceback.extract_tb(err.__traceback__)[-1].lineno}
finally:
    record(mode,report)
print(mode,report["status"])
sys.exit(report["status"] != "PASS")
