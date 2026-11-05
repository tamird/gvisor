"""Check zero dispatch and measure the matched 32-packet wire Save."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import traceback
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse
import re
import subprocess

ROOT = Path(os.environ["RUNNER_TEMP"]).resolve()
PUBLIC = ROOT / "out" / "verdict.json"
CARRIER = os.environ["GITHUB_SHA"]
FIXED = "55a01989ca2fb31a787ccb4dd9b961033bb8e331"
BASE = "1f4905b21f956027a76ded34b96584e00f9269f1"
MUTANT = "1708115482d1a05c80c376bbef47b33cc1f6c980"
ARMS = {"before":"0d3861df2ceb54908c61f3067bc4054b34d15d82", "after":"d8fe8e3ef911f2cde8acfcdaf0c67f318208bd5b"}
REPORTER = "2b51abee8530385431b7225198fdd5cd65e78696"
UDP = "//pkg/tcpip/transport/udp:udp_test"
OUTER = "//pkg/state/tests:tests_test"
INNER = "//pkg/state:state_test"
PAIR = "//test/rbe/state_array_pair:paired_test"
PKG = Path("test/rbe/state_array_pair")
TARGET = "@org_golang_x_perf//cmd/benchstat"
DEADLINE = time.monotonic() + 7100
RACE = ["--config=race", "--extra_toolchains=@llvm//toolchain:linux_x86_64_to_linux_aarch64"]
SCOPED = ["--config=x86_64", "--lockfile_mode=off", "--jobs=4", "--local_resources=memory=8192",
          "--remote_cache=", "--remote_executor=", "--bes_backend=",
          "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox"]
TEST = ["--strategy=TestRunner=local", "--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1",
        "--test_sharding_strategy=disabled", "--test_timeout=150", "--test_output=errors"]
# Project only public error classes and locations from the task's own command.
CLUES = ("conflicting declaration", "conflicting types", "cc: no such file", "runtime/cgo",
         "failed to fetch", "failed to download", "no such target", "could not resolve", "no space left",
         "permission denied", "no such file or directory", "no matching toolchains", "missing strict dependencies",
         "cannot be used", "undefined reference", "timeout", "data race", "race detected")


def now():
    return datetime.now(timezone.utc).isoformat()


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def persist():
    R["missing"] = [k for k in R["expected"] if k not in R["observed"]]
    R["verified_at"] = now()
    PUBLIC.write_text(json.dumps(R, sort_keys=True, indent=2) + "\n")


def selected(buf):
    lines = buf.decode(errors="replace").splitlines()
    joined = "\n".join(lines).lower()
    locations = re.findall(r"\b(float_test\.go|types\.go|encode\.go|decode\.go|wire\.go)\D{1,12}(\d{1,5})", joined)
    reasons = []
    for line in lines:
        if not re.search(r"ERROR: |(?:fatal )?error: |RuntimeError: |OSError: |subprocess\.CalledProcessError:|RTNETLINK answers:|TimeoutError:", line, re.I):
            continue
        if re.search(r"https?://|secret|token|password|cookie|authorization|bearer|gh[pousr]_|github_pat_|[?&]sig=", line, re.I):
            continue
        line = re.sub(r"/(?:home|tmp|__w|github)/[^ :]+", "<path>", line)
        reasons.append(line[:240])
        if len(reasons) == 4:
            break
    return {"signals": [s for s in CLUES if s in joined], "reasons": reasons,
            "locations": sorted(set(":".join(v) for v in locations))[:10],
            "sha256": hashlib.sha256(buf).hexdigest()}


def bazel(row, label, args, limit=1200):
    row["started_at"] = now()
    bep = ROOT / (label + ".bep")
    argv = ["timeout", "--signal=INT", "--kill-after=30s", str(max(1, min(limit, int(DEADLINE-time.monotonic())))), "bazel", "--batch", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g"]
    out = ROOT / (label + ".console")
    with out.open("wb") as stream:
        row["native_exit"] = subprocess.run(argv + args + [f"--build_event_json_file={bep}"], stdout=stream, stderr=subprocess.STDOUT).returncode
    row["execution_finished_at"] = now()
    row["console"] = selected(out.read_bytes()) if row["native_exit"] else {"sha256": sha(out)}
    events = []
    if bep.is_file():
        for line in bep.read_text().splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    row["incomplete_bep"] = True
    row["invocations"] = [e["started"]["uuid"] for e in events if "uuid" in e.get("started", {})]
    row["bep_exits"] = [e["finished"]["exitCode"]["code"] for e in events if "exitCode" in e.get("finished", {}) and "code" in e["finished"]["exitCode"]]

    runner_rows = [e["buildMetrics"].get("actionSummary", {}).get("runnerCount", []) for e in events if "buildMetrics" in e]
    if runner_rows:
        row["runners"] = {e["name"]:e["count"] for e in runner_rows[-1] if e["name"] in ("local","linux-sandbox","processwrapper-sandbox","internal","worker","disk cache hit")}
    persist()
    return events


def test_output(item, label, name):
    assert label in (INNER, OUTER, PAIR) and name in ("test.log", "test.xml")
    assert item["name"] == name
    owner = label[2:].replace(":", "/")
    parsed = urlparse(item["uri"])
    assert parsed.scheme == "file" and not (parsed.netloc or parsed.query or parsed.fragment or parsed.params)
    path = Path(unquote(parsed.path))
    assert path.is_absolute() and path.parts[-len(Path(owner).parts)-2:] == ("testlogs", *Path(owner).parts, name)
    # Always open the known owner's report, using the BEP URI only to check identity.
    check = """
      cache=$(realpath -e -- "$HOME/.cache/bazel")
      base=$(realpath -e -- bazel-testlogs)
      [[ $base == "$cache/"*/testlogs ]]
      expected="$base/$1/$2"
      [[ $(realpath -e -- "$expected") == "$expected" ]]
      [[ $(realpath -e -- "$3") == "$expected" ]]
      [[ -f $expected && $(stat -c %s -- "$expected") -lt 6000000 ]]
      cat -- "$expected"
    """
    return subprocess.check_output(["bash", "-eu", "-c", check, "read-test-output", owner, name, str(path)])


def read_attempt(row, events, label, source):
    attempts = [e for e in events if e.get("id", {}).get("testResult", {}).get("label") == label]
    row["test_attempts"] = len(attempts)
    if len(attempts) != 1:
        raise ValueError("expected one real test attempt")
    result = attempts[0]["testResult"]
    row["configuration"] = attempts[0]["id"]["testResult"]["configuration"]["id"]
    info = result.get("executionInfo", {})
    row["test"] = {"target":label, "status":result["status"], "strategy":info.get("strategy"),
                   "cached":bool(result.get("cachedLocally") or info.get("cachedRemotely")), "source":source,
                   "duration_ms":result.get("testAttemptDurationMillis")}
    outputs = {f["name"]: f for f in result.get("testActionOutput", [])}
    buf = test_output(outputs["test.log"], label, "test.log")
    row["log_sha256"] = hashlib.sha256(buf).hexdigest()
    if row["native_exit"] or result["status"] != "PASSED":
        row["test_failure"] = selected(buf)
    persist()
    return buf.decode(errors="replace"), outputs


def checkout(ref):
    git("switch", "--quiet", "--detach", ref)
    assert git("rev-parse", "HEAD") == ref and not git("status", "--porcelain", "--untracked-files=no")


def binary(row, events, target):
    # Follow the actual completed owner's default output group, as in 334abcfd4.
    sets, pending = {}, []
    for event in events:
        ident = event.get("id", {})
        if "namedSet" in ident:
            sets[ident["namedSet"]["id"]] = event["namedSetOfFiles"]
        completed = ident.get("targetCompleted", {})
        if (completed.get("label") == target and not completed.get("aspect")):
            assert event["completed"]["success"]
            for group in event["completed"]["outputGroup"]:
                if group["name"] == "default":
                    pending.extend(group.get("fileSets", []))
    seen, files = set(), []
    while pending:
        key = pending.pop()["id"]
        if key not in seen:
            seen.add(key)
            files.extend(sets[key].get("files", []))
            pending.extend(sets[key].get("fileSets", []))
    item, = [f for f in files if Path(f.get("name", "")).name == target.split(":")[-1]]
    uri = urlparse(item["uri"])
    assert uri.scheme == "file" and not uri.netloc
    path = Path(unquote(uri.path)).resolve(strict=True)
    base = Path("bazel-out").resolve(strict=True)
    base.relative_to((Path.home() / ".cache/bazel").resolve(strict=True))
    relative = path.relative_to(base)
    package, name = target[2:].split(":")
    assert relative.parts[1:2 + len(Path(package).parts)] == ("bin", *Path(package).parts)
    assert path.name == name and path.is_file() and 0 < path.stat().st_size < 512*1024**2
    data = path.read_bytes()
    assert data[:6] == b"\x7fELF\x02\x01"
    row["binary"] = {"sha256":hashlib.sha256(data).hexdigest(), "machine":int.from_bytes(data[18:20], "little"),
                     "race_symbols":{s:(b"\0"+s.encode()+b"\0") in data for s in ("runtime.raceinit","runtime.racefini")}}
    assert row["binary"]["machine"] == 62
    return path



def checked_attempt(row, events, target, ref):
    log, outputs = read_attempt(row, events, target, ref)
    xml = test_output(outputs["test.xml"], target, "test.xml")
    row["xml_sha256"] = hashlib.sha256(xml).hexdigest()
    xml_cases = list(ET.fromstring(xml).iter("testcase"))
    row["xml_cases"] = len(xml_cases)
    row["xml_failures"] = sum(c.find("failure") is not None or c.find("error") is not None for c in xml_cases)
    row["xml_skips"] = sum(c.find("skipped") is not None or c.get("status") == "notrun" for c in xml_cases)
    persist()
    assert row["test"]["strategy"] == "local" and not row["test"]["cached"] and xml_cases and not row["xml_skips"]
    return log.replace("\x16", "")


def units(name, ref):
    checkout(ref)
    race = name == "race"
    red = name == "mutant"
    targets = [OUTER] if red else [OUTER, INNER]
    phase = R["phases"][name]
    phase.update(source=ref, targets=targets)
    filter_arg = "^TestSignedZero$" if red else "^(TestSignedZero|TestZeroWireRepresentations|TestZeroArrayStructHook)$"
    events = bazel(phase, name, ["test", *SCOPED, *(RACE if race else []), *TEST, "--test_arg=-test.v",
                   "--test_arg=-test.run=" + filter_arg, "--test_env=GO_TEST_WRAP_TESTV=1", *targets], 1800 if race else 1050)
    for target in targets:
        row = phase[target] = {"native_exit":phase["native_exit"]}
        log = checked_attempt(row, events, target, ref)
        cases = {case:status for status,case in re.findall(r"^\s*--- (PASS|FAIL|SKIP): (Test[^\s]+) \(",log,re.M)}
        row["outcomes"] = cases
        row["sign_loss"] = re.findall(r"(-0 != 0|\(-0\+0i\) != \(0\+0i\)|\(0-0i\) != \(0\+0i\))",log)[:12]
        persist()
        if target == OUTER:
            signed_cases = {"TestSignedZero", "TestSignedZero/graph", "TestSignedZero/wire"}
            for engine in ("graph", "wire"):
                signed_cases |= {f"TestSignedZero/{engine}/{i}" for i in range(6)}
            assert set(cases) == signed_cases
        else:
            types = ["bool","int64","uint32","float32","float64","complex64","complex128",
                     "string","*int","[]int","map[int]int","interface_{}","[2]int","[1][1]int"]
            assert set(cases) == {"TestZeroArrayStructHook","TestZeroWireRepresentations",
                                  *("TestZeroWireRepresentations/"+t for t in types)}
        if red:
            assert phase["native_exit"] == 3 and row["test"]["status"] == "FAILED" and row["xml_failures"]
            assert cases["TestSignedZero/wire"] == "FAIL" and all(v=="PASS" for k,v in cases.items() if "/graph" in k)
            assert any(cases[f"TestSignedZero/wire/{i}"]=="FAIL" for i in range(4)) and row["sign_loss"]
        else:
            assert phase["native_exit"] == 0 and row["test"]["status"]=="PASSED" and not row["xml_failures"]
            assert set(cases.values()) == {"PASS"}
            binary(row, events, target)
            assert set(row["binary"]["race_symbols"].values()) == {race}
        persist()


def bench_binary(side):
    ref = ARMS[side]
    checkout(ref)
    phase = R["phases"][side]
    phase.update(source=ref, production=BASE if side=="before" else FIXED, target=UDP)
    events = bazel(phase, side, ["build", *SCOPED, "-c", "opt", UDP])
    assert phase["native_exit"] == 0
    path = binary(phase, events, UDP)
    destination = ROOT / (side+"-udp_test")
    shutil.copy2(path,destination)
    assert sha(destination)==phase["binary"]["sha256"]


def reporter():
    checkout(REPORTER)
    assert git("rev-parse", "HEAD:MODULE.bazel") == "1665015b7719a98f2176b44a0f1c298854dadfab"
    assert Path(".bazelversion").read_text().strip()=="8.5.0"
    phase = R["phases"]["reporter"]
    phase.update(source=REPORTER, target=TARGET)
    flags=["--config=x86_64","-c","opt","--jobs=4","--spawn_strategy=local",
           "--strategy=GoStdlib=processwrapper-sandbox","--strategy=GoStdlibList=processwrapper-sandbox",
           "--remote_cache=","--remote_executor=","--bes_backend=","--stamp","--workspace_status_command="]
    bazel(phase, "reporter", ["build", *flags,TARGET], 1100)
    assert phase["native_exit"]==0
    path = subprocess.check_output(["timeout","150","bazel","--batch","--nosystem_rc","--nohome_rc","cquery",*flags,
                                    "--output=files",TARGET],text=True).strip()
    program=Path(path).resolve(strict=True)
    program.relative_to(Path("bazel-out").resolve(strict=True))
    assert program.name=="benchstat" and os.access(program,os.X_OK)
    shutil.copy2(program,ROOT/"benchstat")
    phase["sha256"]=sha(ROOT/"benchstat")


def pairs():
    checkout(CARRIER)
    phase=R["phases"]["pairs"]
    phase.update(source=CARRIER,target=PAIR)
    inputs={}
    for side in ARMS:
        rel=Path("arms",side,"_main/pkg/tcpip/transport/udp/udp_test")
        path=PKG/rel
        path.parent.mkdir(parents=True)
        assert not path.exists()
        shutil.copy2(ROOT/(side+"-udp_test"),path)
        digest=sha(path)
        assert digest==R["phases"][side]["binary"]["sha256"]
        inputs[str(rel)]={"size":path.stat().st_size,"sha256":digest}
    assert len({v["sha256"] for v in inputs.values()})==2
    (PKG/"inputs.json").write_text(json.dumps(inputs))
    shutil.copy2(ROOT/"benchstat",PKG/"benchstat")
    assert sha(PKG/"benchstat")==R["phases"]["reporter"]["sha256"]
    events=bazel(phase,"pairs",["test",*SCOPED,"-c","opt",*TEST,"--test_timeout=420",
                 "--nozip_undeclared_test_outputs",PAIR],700)
    log=checked_attempt(phase,events,PAIR,CARRIER)
    rows=[json.loads(v) for v in re.findall(r"^STATE_SAMPLE (\{[^\n]+\})$",log,re.M)]
    phase["samples"]=rows
    phase["child_exits"]=[json.loads(v) for v in re.findall(r"^PAIR_EXIT (\{[^\n]+\})$",log,re.M)]
    persist()
    plan=[(side,False,f"warmup-{side}-save_udp") for side in ARMS]
    for i in range(16):
        plan.extend((side,True,f"sample-{i:02d}-{side}-save_udp") for side in
                    (("before","after") if i%2==0 else ("after","before")))
    assert [(r["side"],r["measured"],r["name"]) for r in rows]==plan
    assert all(r["status"]==0 and r["binary_sha256"]==R["phases"][r["side"]]["binary"]["sha256"] for r in rows)
    assert len({(v["wire_objects"],v["wire_sha256"]) for v in rows})==1
    assert phase["native_exit"]==0 and phase["test"]["status"]=="PASSED" and phase["xml_failures"]==0
    assert phase["xml_cases"]==len(rows)
    for side in ARMS:
        lines=[r["row"] for r in rows if r["side"]==side and r["measured"]]
        assert len(lines)==16 and all(re.fullmatch(r"BenchmarkUDPReceiveQueueSave/32(?:-1)?\s+\d+[\s\d.a-zA-Z+/%-]+",l) for l in lines)
        (PUBLIC.parent/(side+".bench")).write_text("goos: linux\ngoarch: amd64\npkg: gvisor.dev/gvisor/pkg/tcpip/transport/udp\n"+"\n".join(lines)+"\n")
    output=PUBLIC.parent/"benchstat.txt"
    with output.open("w") as stream:
        subprocess.run([str(ROOT/"benchstat"),"before.bench","after.bench"],cwd=PUBLIC.parent,stdout=stream,check=True,timeout=60)
    stats=output.read_text()
    assert "UDPReceiveQueueSave/32" in stats
    assert re.search(r"n=16|n1=n2=16",stats)
    phase["benchstat_sha256"]=sha(output)


R={"status":"INCOMPLETE","fixed":FIXED,"base":BASE,"mutant":MUTANT,"arms":ARMS,"carrier":CARRIER,
   "expected":["normal","race","mutant","before","after","reporter","pairs"],"observed":[],"phases":{},
   "errors":{},"started_at":now()}
persist()
assert git("rev-parse","HEAD")==CARRIER and git("rev-parse",CARRIER+"^")==ARMS["before"]
assert git("rev-parse",FIXED+"^")==BASE and git("rev-parse",MUTANT+"^")==FIXED
assert git("diff","--name-only",MUTANT,FIXED)=="pkg/state/encode.go"
fixed_code=git("show",FIXED+":pkg/state/encode.go")
arithmetic=fixed_code.replace("how == encodeDefault && math.Float64bits(v) == 0","how == encodeDefault && v == 0").replace("how == encodeDefault && isPositiveZeroComplex(v)","how == encodeDefault && v == 0")
assert arithmetic!=fixed_code and arithmetic==git("show",MUTANT+":pkg/state/encode.go")
fixture=["pkg/tcpip/transport/udp/BUILD","pkg/tcpip/transport/udp/codec_queue_benchmark_test.go"]
for side,production in (("before",BASE),("after",FIXED)):
    assert git("diff","--name-only",production,ARMS[side]).splitlines()==fixture
assert not git("diff","--name-only",*ARMS.values(),"--",*fixture)
for name,operation in (("normal",lambda:units("normal",FIXED)),("race",lambda:units("race",FIXED)),
                       ("mutant",lambda:units("mutant",MUTANT)),("after",lambda:bench_binary("after")),
                       ("before",lambda:bench_binary("before")),("reporter",reporter),("pairs",pairs)):
    R["phases"][name]={}
    try:
        operation()
    except Exception as exc:
        R["errors"][name]={"type":type(exc).__name__,"lines":[v.lineno for v in traceback.extract_tb(exc.__traceback__) if Path(v.filename).name=="state_zero.py"]}
    else:
        R["observed"].append(name)
    persist()
R["status"]="PASS" if not R["missing"] and not R["errors"] else "FAIL"
persist()
assert R["status"]=="PASS"
