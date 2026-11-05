"""Check wire destination conversions and interface wait ordering on two rebased decoder proposals."""

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
BASE = "e1ec0fd6a841da016483565d9f6548196c1c835b"
DEST_OLD = "b45fba8d8a303c438706d3c00b174f2736193731"
DEST_NEW = "b3d4c616328d4b266091f465628cb074d4afaa2f"
WAIT_OLD = "268638e5a85cbfe0f0c22e024be79a4e7087748f"
WAIT_NEW = "27451d0c2c88e30eee03cf92ae9252600a7d40c7"
STATE = "//pkg/state:state_test"
TESTS = "//pkg/state/tests:tests_test"
DEST_FILE = "pkg/state/types_test.go"
WAIT_FILE = "pkg/state/tests/load_test.go"
DESTINATION = "TestLegacyValueDestinations"
CONVERSIONS = ("array_in_slice", "interface_in_scalar", "nil_interface_in_scalar")
WAITING = "TestInterfaceWaitOrdering"
OLD_WAIT = ("TestLoadHooks/load-hooks2", "TestLoadHooks/load-hooks9")
DEST_CASES = [DESTINATION, *(DESTINATION + "/" + s for s in CONVERSIONS)]
NEW_WAIT_CASES = [WAITING] + [WAITING + "/" + mode + suffix
                              for mode in ("wire", "graph") for suffix in ("", "/0", "/1")]
PHASES = {
    "destination_before": DEST_OLD, "destination_after": DEST_NEW,
    "wait_before": WAIT_OLD, "wait_after": WAIT_NEW,
    "destination_before_frame": DEST_OLD, "destination_after_frame": DEST_NEW,
}
TREES = {
    DEST_OLD: "ee9be2211404e7b04002ebd43f9358cf6ff26791",
    DEST_NEW: "54ddd463d163b12702f949c036382a2c50f3bd62",
    WAIT_OLD: "0403f077532c20797e40b3a6fc460d35e4725517",
    WAIT_NEW: "cb289e3a91f756f47912a82ea34003991aef4ff5",
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
    lines = buf.decode(errors="replace").splitlines()
    joined = "\n".join(lines).lower()
    locations = re.findall(r"\b(types_test\.go|load_test\.go|tests\.go|types\.go|encode\.go|decode\.go|wire\.go)\D{1,12}(\d{1,5})", joined)
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
    argv = ["timeout", "--signal=INT", "--kill-after=30s", "1450" if label == "destination_before" else "430", "bazel", "--batch", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g"]
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
    assert label in (STATE, TESTS) and name in ("test.log", "test.xml")
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


def binary(row, events, owner):
    # Follow the actual completed owner's default output group, as in 334abcfd4.
    sets, pending = {}, []
    for event in events:
        ident = event.get("id", {})
        if "namedSet" in ident:
            sets[ident["namedSet"]["id"]] = event["namedSetOfFiles"]
        target = ident.get("targetCompleted", {})
        if (target.get("label") == owner and not target.get("aspect") and
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
    package, executable = owner[2:].split(":")
    item, = [f for f in files if Path(f.get("name", "")).name == executable]
    uri = urlparse(item["uri"])
    assert uri.scheme == "file" and not uri.netloc
    path = Path(unquote(uri.path)).resolve(strict=True)
    base = Path("bazel-out").resolve(strict=True)
    base.relative_to((Path.home() / ".cache/bazel").resolve(strict=True))
    relative = path.relative_to(base)
    assert relative.parts[1:2 + len(Path(package).parts)] == ("bin", *Path(package).parts)
    assert path.name == executable and path.is_file() and 0 < path.stat().st_size < 512*1024**2
    data = path.read_bytes()
    assert data[:6] == b"\x7fELF\x02\x01"
    row["binary"] = {"sha256":hashlib.sha256(data).hexdigest(), "machine":int.from_bytes(data[18:20], "little"),
                     "race_symbols":{s:(b"\0"+s.encode()+b"\0") in data for s in ("runtime.raceinit","runtime.racefini")}}
    assert row["binary"]["machine"] == 62


def observe_destination():
    # Inspect a copy after the original Save and before the normal Load.
    path = Path(DEST_FILE)
    before = path.read_text()
    marker = "\t\t\tif _, err := Load(t.Context(), &buf, test.destination); err != nil {"
    assert before.count(marker) == 1
    extra = '''\t\t\thdr, err := ReadHeaderMode(&wire.Reader{Reader: bytes.NewReader(buf.Bytes())})
\t\t\tif err != nil {
\t\t\t\tt.Fatal(err)
\t\t\t}
\t\t\tt.Logf("destination frame %s %t %t %d %d", test.name, hdr.Object, hdr.Graph, hdr.Length, buf.Len())
'''
    path.write_text(before.replace(marker, extra + marker))
    return git("hash-object", DEST_FILE)


def wait_frames(log, names):
    # runTestCases already prints its saved bytes. Parse just the real first
    # unsigned-varint header for each selected serial Go subtest.
    frames = {}
    current = None
    waiting = False
    for line in log.splitlines():
        match = re.match(r"^=== (?:RUN|NAME)\s+(Test\S+)", line)
        if match:
            current = match[1]
            waiting = False
        if current not in names:
            continue
        if line.rstrip().endswith("Raw state:"):
            waiting = True
            continue
        if waiting:
            raw = re.search(r"\[([0-9 ]+)\]\s*$", line)
            if not raw:
                continue
            payload = bytes(map(int, raw[1].split()))
            n = 0
            for index, byte in enumerate(payload[:10]):
                n |= (byte & 127) << (7 * index)
                if byte < 128:
                    break
            else:
                raise ValueError("invalid frame header")
            assert n < (1 << 64) and current not in frames
            frames[current] = {"object": bool(n >> 63), "graph": bool(n & (1 << 62)),
                               "length": n & ((1 << 62) - 1), "bytes": len(payload)}
            waiting = False
    return frames


def selected_xml(xml):
    named = {}
    for case in ET.fromstring(xml).iter("testcase"):
        name = case.get("name", "")
        if not name.startswith("Test"):
            continue
        if case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "suppressed":
            outcome = "SKIP"
        elif case.find("failure") is not None or case.find("error") is not None:
            outcome = "FAIL"
        else:
            outcome = "PASS"
        assert name not in named
        named[name] = outcome
    return named


def execute(name, src):
    destination = name.startswith("destination")
    traced = name.endswith("_frame")
    before = "before" in name
    target = STATE if destination else TESTS
    if destination:
        selector = "^" + DESTINATION + "$"
        cases = DEST_CASES
    elif before:
        selector = "^TestLoadHooks$/^load-hooks(2|9)$"
        cases = ["TestLoadHooks", *OLD_WAIT]
    else:
        selector = "^" + WAITING + "$"
        cases = NEW_WAIT_CASES
    checkout(src)
    row = R["phases"][name] = {"source": src, "target": target, "cases": cases}
    if traced:
        row["instrumented_blob"] = observe_destination()
        assert git("diff", "--name-only") == DEST_FILE
        persist()
    try:
        events = bazel(row, name, ["test", *SCOPED, *TEST, "--test_arg=-test.v",
                                  "--test_arg=-test.run=" + selector, "--test_env=GO_TEST_WRAP_TESTV=1", target])
    finally:
        if traced:
            git("restore", "--", DEST_FILE)
    raw, outputs = read_attempt(row, events, target, src)
    log = raw.replace("\x16", "")
    xml = test_output(outputs["test.xml"], target, "test.xml")
    row["xml_sha256"] = hashlib.sha256(xml).hexdigest()
    row["cases_observed"] = selected_xml(xml)
    row["go_results"] = dict((case, status) for status, case in re.findall(
        r"^\s*--- (PASS|FAIL|SKIP): (Test[^\s]+) \(", log, re.M))
    if destination:
        row["messages"] = [m[:180] for m in re.findall(r"types_test\.go:\d+: ([^\n]+)", log)
                           if "destination frame" not in m and re.fullmatch(r"[\w .*(),:=\[\]-]{1,220}", m)][:9]
        if traced:
            rows = re.findall(r"destination frame (\w+) (true|false) (true|false) (\d+) (\d+)", log)
            row["frames"] = {site: {"object": obj == "true", "graph": graph == "true",
                                    "length": int(length), "bytes": int(size)} for site, obj, graph, length, size in rows}
            row["frame_count"] = len(rows)
    else:
        leaves = OLD_WAIT if before else [case for case in cases if case.endswith(("/0", "/1"))]
        row["frames"] = wait_frames(log, leaves)
    persist()
    binary(row, events, target)
    persist()
    assert row["test"]["strategy"] == "local" and not row["test"]["cached"]
    assert row["go_results"] == row["cases_observed"] and set(row["cases_observed"]) == set(cases)
    observed = set(row["cases_observed"].values())
    assert "SKIP" not in observed
    if "FAIL" in observed:
        assert destination and before and row["native_exit"] == 3 and row["test"]["status"] == "FAILED"
    else:
        assert observed == {"PASS"} and row["native_exit"] == 0 and row["test"]["status"] == "PASSED"
    if traced:
        assert row["frame_count"] == 3 and set(row["frames"]) == set(CONVERSIONS)
        assert all(frame["graph"] == before for frame in row["frames"].values())
        original = R["phases"][name.removesuffix("_frame")]
        assert row["cases_observed"] == original["cases_observed"] and row["native_exit"] == original["native_exit"]
    if not destination:
        assert set(row["frames"]) == set(leaves)
        assert all(frame["graph"] == (before or "/graph/" in case) for case, frame in row["frames"].items())
    assert all(frame["object"] and frame["length"] > 0 and frame["bytes"] > 0 for frame in row.get("frames", {}).values())
    R["observed"].append(name)
    persist()


R = {"status": "INCOMPLETE", "base": BASE, "carrier": CARRIER, "expected": list(PHASES),
     "observed": [], "phases": {}, "errors": {}, "started_at": now()}
persist()
assert git("rev-parse", "HEAD") == CARRIER
assert git("rev-parse", CARRIER + "^") == WAIT_NEW
assert git("rev-parse", WAIT_NEW + "^") == WAIT_OLD
assert git("rev-parse", WAIT_OLD + "^") == DEST_NEW
assert git("rev-parse", DEST_NEW + "^") == DEST_OLD
assert git("rev-parse", DEST_OLD + "^^") == BASE
assert git("diff", "--name-only", DEST_OLD, DEST_NEW) == DEST_FILE
assert git("diff", "--name-only", WAIT_OLD, WAIT_NEW) == WAIT_FILE
for src, tree in TREES.items():
    assert git("rev-parse", src + "^{tree}") == tree
for name, src in PHASES.items():
    try:
        execute(name, src)
    except Exception as exc:
        import traceback
        R["errors"][name] = {"type": type(exc).__name__, "lines": [
            v.lineno for v in traceback.extract_tb(exc.__traceback__) if Path(v.filename).name == "state_decoders.py"]}
    persist()
R["status"] = "PASS" if not R["missing"] and not R["errors"] else "FAIL"
persist()
assert R["status"] == "PASS"
