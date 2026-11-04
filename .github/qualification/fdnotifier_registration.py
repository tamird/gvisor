"""Run the unfiltered registration and startup tests on the reconciled source."""

from datetime import datetime, timezone
import hashlib
import json
import os
import platform
from pathlib import Path
from urllib.parse import unquote, urlparse
import re
import subprocess
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(os.environ["RUNNER_TEMP"]).resolve()
PUBLIC = ROOT / "out" / "verdict.json"
CARRIER = os.environ["GITHUB_SHA"]
FIXED = "e6bb7ceffc585bbf1d336468d094a880935842c8"
BASE = "007bd6c1a9f79faf15bb32daaff382ec1af5ab63"
UNIT = "//pkg/fdnotifier:fdnotifier_test"
PKG = Path("pkg/fdnotifier")
MUST_RUN = {"TestStaleEpollEvent", "TestStaleEpollEvent/ReusedFD", "TestStaleEpollEvent/RearmedFD",
            "TestNotifierStartup", "TestNotifierStartup/Pause", "TestNotifierStartup/Register"}
BLOBS = {"BUILD": "46453d749f20ae3d542bdd6bba58069b7ccec308",
         "fdnotifier.go": "60d684a9cc1d49431ddeceaafc16aa470ec825ea",
         "fdnotifier_test.go": "dfaacab27fa34bd0790d4a6f90737130e2040a80"}
STARTUP = "6904fb781e52d6d9870c3251a4451eefda3c8cca"
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
    joined = buf.decode(errors="replace").lower()
    return {"signals": [s for s in CLUES if s in joined],
            "sha256": hashlib.sha256(buf).hexdigest()}


def bazel(row, label, args):
    row["started_at"] = now()
    row["requested_invocation"] = str(uuid.uuid4())
    persist()
    bep = ROOT / (label + ".bep")
    argv = ["timeout", "--signal=INT", "--kill-after=30s", "1440", "bazel", "--batch", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g"]
    out = ROOT / (label + ".console")
    with out.open("wb") as stream:
        row["native_exit"] = subprocess.run(argv + args + [f"--build_event_json_file={bep}", "--invocation_id=" + row["requested_invocation"]], stdout=stream, stderr=subprocess.STDOUT).returncode
    row["execution_finished_at"] = now()
    with out.open("rb") as stream:
        stream.seek(max(0, out.stat().st_size - 1048576))
        row["console"] = selected(stream.read())
    row["console"]["whole_sha256"] = sha(out)
    assert not bep.is_file() or bep.stat().st_size < 128*1024**2
    events = [json.loads(s) for s in bep.read_text().splitlines() if s.strip()] if bep.is_file() else []
    starts = [e["started"]["uuid"] for e in events if "started" in e]
    row["observed_invocations"] = starts
    assert not starts or starts == [row["requested_invocation"]]
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
                   "duration_ms":result.get("testAttemptDurationMillis"),
                   "started_millis":result.get("testAttemptStartMillisEpoch"),
                   "started":result.get("testAttemptStart"), "duration":result.get("testAttemptDuration")}
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
    item, = [f for f in files if Path(f.get("name", "")).name == "fdnotifier_test"]
    uri = urlparse(item["uri"])
    assert uri.scheme == "file" and not uri.netloc
    path = Path(unquote(uri.path)).resolve(strict=True)
    base = Path("bazel-out").resolve(strict=True)
    base.relative_to((Path.home() / ".cache/bazel").resolve(strict=True))
    relative = path.relative_to(base)
    assert relative.parts[1:4] == ("bin", "pkg", "fdnotifier")
    assert path.name == "fdnotifier_test" and path.is_file() and 0 < path.stat().st_size < 512*1024**2
    data = path.read_bytes()
    assert data[:6] == b"\x7fELF\x02\x01"
    row["binary"] = {"sha256":hashlib.sha256(data).hexdigest(), "machine":int.from_bytes(data[18:20], "little"), "bytes":len(data)}
    assert row["binary"]["machine"] == 62



def execute():
    checkout(FIXED)
    row = R["phases"]["fdnotifier"] = {"source": FIXED, "target": UNIT, "blobs": BLOBS}
    assert all(git("rev-parse", f"{FIXED}:{PKG}/{f}") == blob for f, blob in BLOBS.items())
    assert git("diff", "--name-only", BASE, FIXED).splitlines() == [str(PKG / f) for f in BLOBS]
    owner, = re.findall(r'go_test\(\s+name = "fdnotifier_test",.*?srcs = \[(.*?)\]', (PKG / "BUILD").read_text(), re.S)
    sources = re.findall(r'"(\w+_test\.go)"', owner)
    expected = {case for f in sources for case in re.findall(r"^func (Test[A-Z0-9_]\w*)\(\s*(?:\w+\s+)?\*testing\.T\)", (PKG / f).read_text(), re.M)}
    assert len(sources) == 2 and len(expected) == 2
    events = bazel(row, "fdnotifier", ["test", *SCOPED, *TEST, "--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1", UNIT])
    log, outputs = read_attempt(row, events, UNIT, FIXED)
    xml = test_output(outputs["test.xml"], UNIT, "test.xml")
    row["xml_sha256"] = hashlib.sha256(xml).hexdigest()
    cases = list(ET.fromstring(xml).iter("testcase"))
    names = [case.get("name", "") for case in cases]
    valid = all(re.fullmatch(r"[A-Za-z0-9_./#=+-]{1,180}", name) for name in names)
    row["case_count"] = len(cases)
    row["failures"] = sum(any(case.find(tag) is not None for tag in ("error", "failure")) for case in cases)
    row["skipped"] = sum(case.find("skipped") is not None or case.get("status") in {"notrun", "suppressed"} or case.get("result") == "suppressed" for case in cases)
    row["required"] = {name: names.count(name) for name in sorted(MUST_RUN)}
    if valid:
        row["cases"] = names
    persist()
    assert row["native_exit"] == 0 and row["test"]["status"] == "PASSED"
    assert row["test"]["strategy"] == "local" and not row["test"]["cached"]
    assert not row["skipped"] and not row["failures"] and len(set(names)) == len(cases)
    assert expected == {n for n in names if "/" not in n}
    assert set(names) == MUST_RUN
    binary(row, events)
    R["observed"].append("fdnotifier")


R = {"status":"INCOMPLETE", "fixed":FIXED, "base":BASE, "carrier":CARRIER,
     "expected":["fdnotifier"], "observed":[], "phases":{}, "errors":{}, "started_at":now(),
     "scope":"Unfiltered fdnotifier package unit owner, native GNU/Linux AMD64, no race or analyzer claim",
     "environment":{"system":platform.system(), "machine":platform.machine(), "bazel":"9.3.1"}}
persist()
try:
    assert R["environment"]["system"] == "Linux" and R["environment"]["machine"] == "x86_64"
    assert git("rev-parse", "HEAD") == CARRIER and git("rev-parse", CARRIER+"^") == FIXED
    assert git("rev-parse", FIXED+"^") == BASE
    assert git("rev-parse", f"{BASE}:{PKG}/startup_test.go") == git("rev-parse", f"{FIXED}:{PKG}/startup_test.go") == STARTUP
    execute()
except Exception as exc:
    import traceback
    R["errors"]["fdnotifier"] = {"type":type(exc).__name__,"lines":[v.lineno for v in traceback.extract_tb(exc.__traceback__) if Path(v.filename).name == "fdnotifier_registration.py"]}
R["status"] = "PASS" if R["observed"] == R["expected"] and not R["errors"] else "FAIL"
persist()
assert R["status"] == "PASS"
