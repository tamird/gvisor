"""Check the signed-zero product’s native ordinary and race tests."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import traceback
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse
import re
import subprocess

ROOT = Path(os.environ["RUNNER_TEMP"]).resolve()
PUBLIC = ROOT / "out" / "verdict.json"
CARRIER = os.environ["GITHUB_SHA"]
FIXED = "13905351476c169f99b8e3c3a9203fb11203a0ad"
BASE = "1f4905b21f956027a76ded34b96584e00f9269f1"
OUTER = "//pkg/state/tests:tests_test"
INNER = "//pkg/state:state_test"
DEADLINE = time.monotonic() + 3200
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
    go_errors = []
    for line in lines:
        entry = re.search(r"\b(types_test\.go|float_test\.go|encode\.go):(\d+):(\d+): ([^\n]+)",line)
        if entry:
            detail = entry[4]
            if not re.fullmatch(r"[a-zA-Z0-9_ @.():,*/=+{}\[\]<>-]{1,180}",detail):
                detail = "compiler diagnostic present"
            go_errors.append({"file":entry[1],"line":int(entry[2]),"column":int(entry[3]),"error":detail})
    return {"go_errors":go_errors[:8], "signals": [s for s in CLUES if s in joined], "reasons": reasons,
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
    assert label in (INNER, OUTER) and name in ("test.log", "test.xml")
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


def check_cases(name, phase, events, target):
    row = phase[target] = {"native_exit":phase["native_exit"]}
    log = checked_attempt(row, events, target, FIXED)
    cases = {case:status for status,case in re.findall(r"^\s*--- (PASS|FAIL|SKIP): (Test[^\s]+) \(",log,re.M)}
    row["outcomes"] = cases
    persist()
    if target == OUTER:
        expected = {"TestSignedZero", "TestSignedZero/graph", "TestSignedZero/wire"}
        for engine in ("graph", "wire"):
            expected |= {f"TestSignedZero/{engine}/{i}" for i in range(6)}
    else:
        types = ["bool", "int64", "uint32", "float32", "float64", "complex64", "complex128",
                 "string", "*int", "[]int", "map[int]int", "interface_{}", "[2]int", "[1][1]int"]
        expected = {"TestZeroArrayStructHook", "TestZeroWireRepresentations",
                    *("TestZeroWireRepresentations/"+typ for typ in types)}
    assert set(cases)==expected and set(cases.values())=={"PASS"}
    assert row["test"]["status"]=="PASSED" and not row["xml_failures"] and row["xml_cases"]==len(expected)
    binary(row,events,target)
    assert set(row["binary"]["race_symbols"].values())=={name=="race"}


def check_profile(name):
    checkout(FIXED)
    targets=[OUTER, INNER]
    phase=R["phases"][name]={"source":FIXED,"targets":targets,"verified":[],"owner_errors":{}}
    events=bazel(phase,name,["test",*SCOPED,*(RACE if name=="race" else []),*TEST,"--keep_going",
                    "--test_arg=-test.v","--test_arg=-test.run=^(TestSignedZero|TestZeroWireRepresentations|TestZeroArrayStructHook)$",
                    "--test_env=GO_TEST_WRAP_TESTV=1",*targets],1800 if name=="race" else 1200)
    for target in targets:
        try:
            check_cases(name,phase,events,target)
        except Exception as exc:
            phase["owner_errors"][target]=error(exc)
        else:
            phase["verified"].append(target)
        persist()
    assert phase["native_exit"]==0 and phase["verified"]==targets and not phase["owner_errors"]


def error(exc):
    return {"type":type(exc).__name__,"lines":[v.lineno for v in traceback.extract_tb(exc.__traceback__) if Path(v.filename).name=="state_zero.py"]}


R={"status":"INCOMPLETE","fixed":FIXED,"base":BASE,"carrier":CARRIER,"prior_artifact":13423159446,
   "prior_sha256":"1ff569eb2a3c24b66d08cd6f4216e861818abe91983a0e6180234bbb1a8ae1e3",
   "expected":["normal","race"],"observed":[],"phases":{},"errors":{},"started_at":now()}
persist()
assert git("rev-parse","HEAD")==CARRIER and git("rev-parse",CARRIER+"^")==FIXED and git("rev-parse",FIXED+"^")==BASE
assert git("rev-parse",FIXED+":pkg/state/encode.go")=="70ae7b992c46c4945e3ff21d923a99e8692c58a1"
assert git("rev-parse",FIXED+":pkg/tcpip/transport/udp")=="08e8241cfdaeb1a97f367c508f9e7d3a067cfaed"
for name in R["expected"]:
    try:
        check_profile(name)
    except Exception as exc:
        R["errors"][name]=error(exc)
    else:
        R["observed"].append(name)
    persist()
if len(R["observed"])==2:
    for target in (OUTER,INNER):
        ordinary,raced=(R["phases"][name][target] for name in R["expected"])
        assert ordinary["configuration"]!=raced["configuration"] and ordinary["binary"]["sha256"]!=raced["binary"]["sha256"]
R["status"]="PASS" if not R["missing"] and not R["errors"] else "FAIL"
persist()
assert R["status"]=="PASS"
