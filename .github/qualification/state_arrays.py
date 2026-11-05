"""Check the original and revised reference fixtures and read their real object frames."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlparse
import re
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(os.environ["RUNNER_TEMP"]).resolve()
PUBLIC = ROOT / "out" / "verdict.json"
CARRIER = os.environ["GITHUB_SHA"]
FIXED = "e1ec0fd6a841da016483565d9f6548196c1c835b"
STOCK = "70df168c4eda6463a3ff47dd635aa2e6a6c90a2a"
FIXED_TRACE = "4c958dc4b731bc24654c1c19379fdfdac18c2d51"
STOCK_TRACE = "f9eccc218f5c9a588c03279d8612a49d1d596ce1"
BASE = "e00f572e3deaea5f1ce6b455d8d72d7749e0f359"
UNIT = "//pkg/state/tests:tests_test"
CASES = ("TestReferenceArrayWire", "TestReferenceArraySnapshotTiming", "TestReferenceArrayLateParent")
SITES = {"addressable": CASES[0], "by_value": CASES[0], "snapshot": CASES[1], "late_parent": CASES[2]}
FIXTURE = "pkg/state/tests/reference_array_test.go"
PHASES = (
    ("stock", STOCK, "e69b9c6c94bed51bcf75c17ad217278bfd9a7c7b"),
    ("fixed", FIXED, "595e4c23133ae23497f1230ed871cb45a0e86be9"),
    ("stock_frame", STOCK_TRACE, "e9bcfe38c76e89972ed157747262fd1d2fc5045e"),
    ("fixed_frame", FIXED_TRACE, "c9249cd302ca538100842b59c549dbed57c1ae2c"),
)
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
    locations = re.findall(r"\b(reference_array_test\.go|tests\.go|types\.go|encode\.go|decode\.go|wire\.go)\D{1,12}(\d{1,5})", joined)
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
    argv = ["timeout", "--signal=INT", "--kill-after=30s", "1600" if label == "stock" else "700", "bazel", "--batch", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g"]
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
      [[ -f $expected && $(stat -c %s -- "$expected") -lt 32000000 ]]
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
    row = R["phases"][name] = {"source": source, "target": UNIT, "cases": list(CASES)}
    pattern = "^(" + "|".join(CASES) + ")$"
    events = bazel(row, name, ["test", *SCOPED, *TEST, "--test_arg=-test.v",
                               "--test_arg=-test.run=" + pattern, "--test_env=GO_TEST_WRAP_TESTV=1", UNIT])
    log, outputs = read_attempt(row, events, UNIT, source)
    log = log.replace("\x16", "")
    outcomes = re.findall(r"^\s*--- (PASS|FAIL|SKIP): (Test[^\s]+) \(", log, re.M)
    row["cases_observed"] = {case: status for status, case in outcomes}
    row["counts"] = dict(Counter(status for status, _ in outcomes))
    notes = re.findall(r"reference_array_test\.go:(\d+): ([^\n]{1,220})", log)
    row["messages"] = [[line, body] for line, body in notes if re.search(r"want |[Ee]rror|panic|failed", body)
                       and not re.search(r"https?://|token|secret|password|authorization|cookie", body, re.I)][:8]
    row["frames"] = [
        {"case": case, "site": site, "object": obj == "true", "graph": graph == "true",
         "length": int(length), "bytes": int(size)}
        for case, site, obj, graph, length, size in re.findall(
            r"reference frame case=(Test\w+) site=(\w+) object=(true|false) graph=(true|false) length=(\d+) bytes=(\d+)", log)
    ]
    persist()
    xml = test_output(outputs["test.xml"], UNIT, "test.xml")
    row["xml_sha256"] = hashlib.sha256(xml).hexdigest()
    leaves = list(ET.fromstring(xml).iter("testcase"))
    row["xml_cases"] = len(leaves)
    persist()
    binary(row, events)
    persist()
    assert leaves and row["test"]["strategy"] == "local" and not row["test"]["cached"]
    assert len(outcomes) == len(CASES) and row["cases_observed"].keys() == set(CASES)
    assert all(status in ("PASS", "FAIL") for status, _ in outcomes)
    if all(status == "PASS" for status, _ in outcomes):
        assert row["native_exit"] == 0 and row["test"]["status"] == "PASSED"
    else:
        assert name.startswith("stock") and row["native_exit"] == 3 and row["test"]["status"] == "FAILED"
    if name.endswith("_frame"):
        assert len(row["frames"]) == len(SITES)
        assert {r["site"]: r["case"] for r in row["frames"]} == SITES
        assert all(r["object"] and r["length"] > 0 and r["bytes"] > 0 for r in row["frames"])
        assert all(r["graph"] == name.startswith("stock") for r in row["frames"])
        original = R["phases"][name.removesuffix("_frame")]
        assert row["cases_observed"] == original["cases_observed"] and row["native_exit"] == original["native_exit"]
    else:
        assert not row["frames"]
    R["observed"].append(name)
    persist()


R = {"status": "INCOMPLETE", "base": BASE, "carrier": CARRIER, "expected": [p[0] for p in PHASES],
     "observed": [], "phases": {}, "errors": {}, "started_at": now()}
persist()
assert git("rev-parse", "HEAD") == CARRIER
for child, parent in zip((CARRIER, STOCK_TRACE, FIXED_TRACE, FIXED, STOCK), (STOCK_TRACE, FIXED_TRACE, FIXED, STOCK, BASE)):
    assert git("rev-parse", child + "^") == parent
for left, right in ((STOCK, FIXED), (FIXED, FIXED_TRACE), (FIXED_TRACE, STOCK_TRACE), (STOCK, STOCK_TRACE)):
    assert git("diff", "--name-only", left, right) == FIXTURE
assert git("rev-parse", STOCK + "^{tree}") == "1e4869350f4754c4c647f06e7c07f2880beb8e23"
assert git("rev-parse", FIXED + "^{tree}") == "e8ebfab286741f2882c84f2505363acd2c04985e"
for old, new in ((STOCK, FIXED), (STOCK_TRACE, FIXED_TRACE)):
    before, after = (git("show", commit + ":" + FIXTURE) for commit in (old, new))
    assert after.count("\n\tuseWireEngine(t)") == 3 and after.replace("\n\tuseWireEngine(t)", "") == before
for name, source, blob in PHASES:
    assert git("rev-parse", source + ":" + FIXTURE) == blob
    try:
        execute(name, source)
    except Exception as exc:
        import traceback
        R["errors"][name] = {"type": type(exc).__name__, "lines": [
            v.lineno for v in traceback.extract_tb(exc.__traceback__) if Path(v.filename).name == "state_arrays.py"]}
    persist()
R["status"] = "PASS" if not R["missing"] and not R["errors"] else "FAIL"
persist()
assert R["status"] == "PASS"
