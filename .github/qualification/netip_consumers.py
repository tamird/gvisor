"""Run the selected multicast, loopback, netfilter and netgofer groups on the reconciled source."""

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
FIXED = "3c13ece942c6cfef73e04d6563bc332b0eb05886"
BASE = "007bd6c1a9f79faf15bb32daaff382ec1af5ab63"
INTEGRATION = "//pkg/tcpip/tests/integration:"
OWNERS = {
    INTEGRATION + "multicast_broadcast_test": ("multicast_broadcast_test.go", ("TestPingMulticastBroadcast",)),
    INTEGRATION + "loopback_test": ("loopback_test.go", ("TestIPv6DisabledLoopback",)),
    "//pkg/sentry/socket/netfilter:netfilter_test": ("addrtype_matcher_test.go", (
        "TestAddrTypeIPv4", "TestAddrTypeIPv6", "TestAddrTypeSourceAndInvert", "TestAddrTypeUnmarshal")),
    "//runsc/gofer/net:netgofer_test": ("netgofer_test.go", (
        "TestRunProxyModeEndToEnd", "TestProxyRejectsDisallowedDestinations")),
}
TREES = {
    "pkg/tcpip/tests/integration": "86f11c62f3f1890df7e0dbc31f7fb760391f3598",
    "pkg/sentry/socket/netfilter": "ffebc3b2ffddbbd25b820b789e657b10972a84e8",
    "runsc/gofer/net": "c612954371023279c1380d694da27c67ddd7b72e",
}
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
    kinds = ("undefined:", "has no field or method", "unknown field", "cannot use", "not enough arguments", "too many arguments", "imported and not used", "expected", "want ", "failed", "timed out", "timeout", "connection refused", "i/o timeout")
    locs = []
    for line in joined.splitlines():
        match = re.search(r"(?:^|[/\s(])((?:[a-z0-9_]+/)*)?([a-z0-9_]+\.go):(\d{1,7})(?::\d+)?\b", line)
        if match and match[2] in {
            "addrtype_matcher.go", "addrtype_matcher_test.go", "netgofer.go", "netgofer_test.go",
            "egressfilter.go", "tcpip.go", "icmp.go", "ipv4.go", "ipv6.go", "stack.go", "nic.go",
            "endpoint.go", "loopback_test.go", "multicast_broadcast_test.go",
        } and len(locs) < 12:
            classes = [kind for kind in kinds if kind in line]
            if classes:
                item = [match[2], int(match[3]), classes]
                if item not in locs:
                    locs.append(item)
    return {"signals": [s for s in CLUES if s in joined], "locations": locs,
            "sha256": hashlib.sha256(buf).hexdigest()}



def bazel(row, label, args):
    row["started_at"] = now()
    row["requested_invocation"] = str(uuid.uuid4())
    persist()
    bep = ROOT / (label + ".bep")
    argv = ["timeout", "--signal=INT", "--kill-after=30s", "2700", "bazel", "--batch", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g"]
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
    assert label in OWNERS and name in ("test.log", "test.xml")
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
    if result["status"] != "PASSED":
        row["test_failure"] = selected(buf)
    persist()
    return buf.decode(errors="replace"), outputs


def checkout(ref):
    git("switch", "--quiet", "--detach", ref)
    assert git("rev-parse", "HEAD") == ref and not git("status", "--porcelain", "--untracked-files=no")


def binary(row, events, label):
    pkg, basename = label[2:].split(":")
    # Follow the actual completed owner's default output group, as in 334abcfd4.
    sets, pending = {}, []
    for event in events:
        ident = event.get("id", {})
        if "namedSet" in ident:
            sets[ident["namedSet"]["id"]] = event["namedSetOfFiles"]
        target = ident.get("targetCompleted", {})
        if (target.get("label") == label and not target.get("aspect") and
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
    item, = [f for f in files if Path(f.get("name", "")).name == basename]
    uri = urlparse(item["uri"])
    assert uri.scheme == "file" and not uri.netloc
    path = Path(unquote(uri.path)).resolve(strict=True)
    base = Path("bazel-out").resolve(strict=True)
    base.relative_to((Path.home() / ".cache/bazel").resolve(strict=True))
    relative = path.relative_to(base)
    assert relative.parts[1:2 + len(Path(pkg).parts)] == ("bin", *Path(pkg).parts)
    assert path.name == basename and path.is_file() and 0 < path.stat().st_size < 512*1024**2
    data = path.read_bytes()
    assert data[:6] == b"\x7fELF\x02\x01"
    row["binary"] = {"sha256":hashlib.sha256(data).hexdigest(), "machine":int.from_bytes(data[18:20], "little"), "bytes":len(data)}
    assert row["binary"]["machine"] == 62



def cases_for(label):
    package = Path(label[2:].split(":")[0])
    filename, groups = OWNERS[label]
    source = (package / filename).read_text()
    result = set()
    for group in groups:
        body, = re.findall(r"^func " + group + r"\(t \*testing\.T\) \{(.*?)^\}", source, re.S | re.M)
        result.add(group)
        result.update(group + "/" + name.replace(" ", "_")
                      for name in re.findall(r'\bname:\s*"([^"]+)"', body))
    return result


def verdict(row, events, label, required):
    _, outputs = read_attempt(row, events, label, FIXED)
    xml = test_output(outputs["test.xml"], label, "test.xml")
    row["xml_sha256"] = hashlib.sha256(xml).hexdigest()
    cases = list(ET.fromstring(xml).iter("testcase"))
    names = [case.get("name", "") for case in cases]
    row["case_count"] = len(names)
    row["failures"] = sum(any(case.find(tag) is not None for tag in ("error", "failure")) for case in cases)
    row["skipped"] = sum(case.find("skipped") is not None or case.get("status") in {"notrun","suppressed"} or case.get("result") == "suppressed" for case in cases)
    row["matched"] = sorted(required.intersection(names))
    row["missing"] = sorted(required.difference(names))
    row["other_count"] = sum(n not in required for n in names)
    persist()
    assert row["test"]["status"] == "PASSED" and row["test"]["strategy"] == "local" and not row["test"]["cached"]
    assert not row["failures"] and not row["skipped"] and len(names) == len(required) and set(names) == required
    binary(row, events, label)


def error(exc):
    import traceback
    return {"type":type(exc).__name__, "lines":[v.lineno for v in traceback.extract_tb(exc.__traceback__) if Path(v.filename).name == "netip_consumers.py"]}


R = {"status":"INCOMPLETE", "fixed":FIXED, "base":BASE, "carrier":CARRIER,
     "expected":list(OWNERS), "observed":[], "phases":{}, "errors":{}, "started_at":now(),
     "scope":"Selected source-defined Go tests; local GNU/Linux AMD64 only",
     "environment":{"system":platform.system(), "machine":platform.machine(), "bazel":"9.3.1"}}
persist()
try:
    assert R["environment"]["system"] == "Linux" and R["environment"]["machine"] == "x86_64"
    assert git("rev-parse","HEAD") == CARRIER and git("rev-parse",CARRIER+"^") == FIXED and git("rev-parse",FIXED+"^") == BASE
    checkout(FIXED)
    assert all(git("rev-parse", FIXED+":"+p) == tree for p,tree in TREES.items())
    required = {label: cases_for(label) for label in OWNERS}
    assert [len(v) for v in required.values()] == [7,3,50,6]
    groups = [group for _, roots in OWNERS.values() for group in roots]
    cmd = R["command"] = {}
    events = bazel(cmd, "netip-consumers", ["test", *SCOPED, *TEST, "--keep_going", "--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.run=^(" + "|".join(groups) + ")$", *OWNERS])
    for label in OWNERS:
        row = R["phases"][label] = {}
        try:
            verdict(row, events, label, required[label])
            R["observed"].append(label)
        except Exception as exc:
            R["errors"][label] = error(exc)
        persist()
    assert cmd["native_exit"] == 0 and cmd["observed_invocations"] == [cmd["requested_invocation"]]
except Exception as exc:
    R["errors"]["command"] = error(exc)
R["status"] = "PASS" if R["observed"] == R["expected"] and not R["errors"] else "FAIL"
persist()
assert R["status"] == "PASS"
