"""Qualify the named legacy signaling-NaN regression on the current base."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlparse
import re
import subprocess

ROOT = Path(os.environ["RUNNER_TEMP"]).resolve()
PUBLIC = ROOT / "out" / "verdict.json"
CARRIER = os.environ["GITHUB_SHA"]
FIXED = "d5d31a74d63cfbe91e57220300601d67122dd313"
STOCK = "bbc09b1441970262a189c707283679b6bea2ce17"
BASE = "6dcadf4a84a07f69e8eb5f1ea9d7b2903bd03bd7"
UNIT = "//pkg/state/tests:tests_test"
CASE = "TestSignalingNaN"
FIXTURE = "pkg/state/tests/float_test.go"
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


def bazel(row, label, args):
    row["started_at"] = now()
    bep = ROOT / (label + ".bep")
    argv = ["timeout", "--signal=INT", "--kill-after=30s", "1440", "bazel", "--batch", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g"]
    out = ROOT / (label + ".console")
    with out.open("wb") as stream:
        row["native_exit"] = subprocess.run(argv + args + [f"--build_event_json_file={bep}"], stdout=stream, stderr=subprocess.STDOUT).returncode
    row["execution_finished_at"] = now()
    row["console"] = selected(out.read_bytes()) if row["native_exit"] else {"sha256": sha(out)}
    events = [json.loads(s) for s in bep.read_text().splitlines() if s.strip()] if bep.is_file() else []
    runner_rows = [e["buildMetrics"].get("actionSummary", {}).get("runnerCount", []) for e in events if "buildMetrics" in e]
    if runner_rows:
        row["runners"] = {e["name"]:e["count"] for e in runner_rows[-1] if e["name"] in ("local","linux-sandbox","processwrapper-sandbox","internal","worker","disk cache hit")}
    persist()
    return events


def test_output(item, label, name):
    assert label == UNIT and name in ("test.log", "test.xml")
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


def binary(row, events):
    # Follow the actual completed owner's default output group, as in 334abcfd4.
    sets, pending = {}, []
    for event in events:
        ident = event.get("id", {})
        if "namedSet" in ident:
            sets[ident["namedSet"]["id"]] = event["namedSetOfFiles"]
        target = ident.get("targetCompleted", {})
        if (target.get("label") == UNIT and not target.get("aspect") and
            target.get("configuration", {}).get("id") == row["configuration"]):
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
    item, = [f for f in files if Path(f.get("name", "")).name == "tests_test"]
    uri = urlparse(item["uri"])
    assert uri.scheme == "file" and not uri.netloc
    path = Path(unquote(uri.path)).resolve(strict=True)
    base = Path("bazel-out").resolve(strict=True)
    base.relative_to((Path.home() / ".cache/bazel").resolve(strict=True))
    relative = path.relative_to(base)
    assert relative.parts[1:5] == ("bin", "pkg", "state", "tests")
    assert path.name == "tests_test" and path.is_file() and 0 < path.stat().st_size < 512*1024**2
    data = path.read_bytes()
    assert data[:6] == b"\x7fELF\x02\x01"
    row["binary"] = {"sha256":hashlib.sha256(data).hexdigest(), "machine":int.from_bytes(data[18:20], "little"),
                     "race_symbols":{s:(b"\0"+s.encode()+b"\0") in data for s in ("runtime.raceinit","runtime.racefini")}}
    assert row["binary"]["machine"] == 62


def execute(name, source):
    checkout(source)
    row = R["phases"][name] = {"source":source, "target":UNIT}
    flags = RACE if name == "race" else []
    events = bazel(row, name, ["test", *SCOPED, *flags, *TEST, "--test_arg=-test.v",
                               "--test_arg=-test.run=^TestSignalingNaN$", "--test_env=GO_TEST_WRAP_TESTV=1", UNIT])
    log, outputs = read_attempt(row, events, UNIT, source)
    row["xml_sha256"] = hashlib.sha256(test_output(outputs["test.xml"], UNIT, "test.xml")).hexdigest()
    row["test2json_markers"] = log.count("\x16")
    log = log.replace("\x16", "")
    row["cases_run"] = re.findall(r"^=== RUN\s+(Test\w+)\s*$", log, re.M)
    row["outcomes"] = re.findall(r"^\s*--- (PASS|FAIL|SKIP): TestSignalingNaN \(", log, re.M)
    observations = []
    for match in re.finditer(r"float_test\.go:(\d+): (sample [01]|complex bits): got ([^,\n]+), want ([^\n]+)", log):
        line, kind, actual, expected = match.groups()
        numbers = lambda field: [int(v,16) for v in re.findall(r"0x([0-9a-fA-F]{8})\b", field)[:2]]
        observations.append({"line":int(line),"kind":kind,"actual":numbers(actual),"expected":numbers(expected)})
    row["bit_observations"] = observations[:10]
    persist()
    assert row["test"]["strategy"] == "local" and not row["test"]["cached"] and row["cases_run"] == [CASE]
    if name == "stock":
        assert row["native_exit"] == 3 and row["test"]["status"] == "FAILED" and row["outcomes"] == ["FAIL"]
        assert {v["kind"] for v in observations} == {"sample 0","sample 1","complex bits"}
        assert len(observations) <= 10
        for obs in observations:
            want = obs["expected"]
            assert want and any(v in (0x7f801234,0xff801234) for v in want)
            assert obs["actual"] == [v|0x400000 if v in (0x7f801234,0xff801234) else v for v in want]
        row["disposition"] = "EXPECTED_FAILURE"
    else:
        assert row["native_exit"] == 0 and row["test"]["status"] == "PASSED" and row["outcomes"] == ["PASS"] and not observations
        binary(row, events)
        if name == "race":
            assert all(row["binary"]["race_symbols"].values())
        row["disposition"] = "PASS"
    R["observed"].append(name)
    persist()


R = {"status":"INCOMPLETE", "stock":STOCK, "fixed":FIXED, "base":BASE, "carrier":CARRIER,
     "expected":["stock","fixed","race"], "observed":[], "phases":{}, "errors":{}, "started_at":now()}
persist()
assert git("rev-parse","HEAD") == CARRIER and git("rev-parse",CARRIER+"^") == STOCK
assert git("rev-parse",STOCK+"^") == FIXED and git("rev-parse",FIXED+"^") == BASE
assert git("diff","--name-only",BASE,STOCK) == FIXTURE and not git("diff","--name-only",STOCK,FIXED,"--",FIXTURE)
for name in R["expected"]:
    try:
        execute(name, STOCK if name == "stock" else FIXED)
    except Exception as exc:
        import traceback
        R["errors"][name] = {"type":type(exc).__name__,"lines":[v.lineno for v in traceback.extract_tb(exc.__traceback__) if Path(v.filename).name == "state_nan.py"]}
    persist()
if not R["missing"]:
    assert R["phases"]["fixed"]["configuration"] != R["phases"]["race"]["configuration"]
    assert R["phases"]["fixed"]["binary"]["sha256"] != R["phases"]["race"]["binary"]["sha256"]
R["status"] = "PASS" if not R["missing"] and not R["errors"] else "FAIL"
persist()
assert R["status"] == "PASS"
