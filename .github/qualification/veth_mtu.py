"""Verify the permanent loopback and veth tests against native Linux and two Sentries."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET
import re
import subprocess

ROOT = Path(os.environ["RUNNER_TEMP"]).resolve()
PUBLIC = ROOT / "out" / "verdict.json"
CARRIER = os.environ["GITHUB_SHA"]
FIXED = "679d20c1ba36918979ce60e0f7619c3c72738d03"
STOCK = "cc65110cb1a021ebc7dd5526fb8334dca9fc6b2f"
BASE = "6dcadf4a84a07f69e8eb5f1ea9d7b2903bd03bd7"
PREVIOUS = "224d478ba5a85e1146267a60e2e7300e92c0b990"
ROUTE = "//test/syscalls:socket_netlink_route_test_"
SHELL = "//test/rtnetlink:setlink_test_"
UNIT = "//pkg/tcpip/link/ethernet:ethernet_test"
CASES = {f"_/NetlinkSetLinkTest.ChangeMTU/{i}" for i in (0, 1)}
SOURCES = ("test/rtnetlink/linux/setlink_test.sh", "test/syscalls/linux/socket_netlink_route.cc")
SCOPED = ["--config=x86_64", "--lockfile_mode=off", "--jobs=4", "--local_resources=memory=8192",
          "--remote_cache=", "--remote_executor=", "--bes_backend=",
          "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox"]
TEST = ["--strategy=TestRunner=local", "--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1",
        "--test_sharding_strategy=disabled", "--test_timeout=150", "--test_output=errors"]
# Exact projection, never export raw logs, packet bytes, paths, or environment.
CLUES = ("conflicting declaration", "conflicting types", "cannot represent machine", "cc: no such file",
         "failed to fetch", "failed to download", "no such target", "could not resolve", "no space left",
         "permission denied", "operation not permitted", "operation not supported", "operation timed out",
         "no such file or directory", "not found", "connection refused", "cannot open network namespace",
         "cannot find device", "not supported", "missing matching veth syn", "peer listener did not start",
         "setting up network", "peer failed", "timed out", "assertionerror", "command not found", "keyerror",
         "no such device", "network is unreachable", "invalid argument", "build did not complete",
         "interface conversion", "exec.exiterror", "setup_container", "netlinksetlinktest", "no tests were run", "not a known option", "not executable", "segmentation fault", "killed")


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
    locations = re.findall(r"\b(socket_netlink_route\.cc|setlink_test\.sh|main\.go|ethernet(?:_test)?\.go|vdso\.cc)\D{1,12}(\d{1,5})", joined)
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
    argv = ["timeout", "--signal=INT", "--kill-after=30s", "2400", "bazel", "--batch", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g"]
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
    assert label in {owner for _, owner in ARMS.values()} and name in ("test.log", "test.xml")
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


ARMS = {
    "native": (STOCK, ROUTE + "native"),
    "stock": (STOCK, ROUTE + "runsc_systrap_directfs"),
    "fixed": (FIXED, ROUTE + "runsc_systrap_directfs"),
    "fixed_shell": (FIXED, SHELL + "runsc_systrap_overlay_directfs"),
    "ethernet": (FIXED, UNIT),
}


def execute(name, source, owner):
    checkout(source)
    row = R["phases"][name] = {"source": source, "target": owner}
    args = []
    if owner.startswith(ROUTE):
        args = ["--test_filter=_/NetlinkSetLinkTest.ChangeMTU/*"]
    elif owner == UNIT:
        args = ["--test_arg=-test.v"]
    events = bazel(row, name, ["test", *SCOPED, *TEST, *args, owner])
    log, outputs = read_attempt(row, events, owner, source)
    test = row["test"]
    assert not test["cached"] and test["strategy"] == "local"
    if owner.startswith(ROUTE):
        xml = test_output(outputs["test.xml"], owner, "test.xml")
        row["xml_sha256"] = hashlib.sha256(xml).hexdigest()
        nodes = list(ET.fromstring(xml).iter("testcase"))
        names = [n.get("classname", "") + "." + n.get("name", "") for n in nodes]
        row["cases"] = names
        row["failure_details"] = {}
        for node, case in zip(nodes, names):
            if case in CASES:
                details = []
                for failure in node.findall("failure")[:3]:
                    message = failure.get("message") or "".join(failure.itertext())
                    details.append({
                        "source_lines": re.findall(r"socket_netlink_route\.cc[:(](\d{1,5})", message)[:4],
                        "numbers": [int(n) for n in re.findall(r"(?:Which is|Actual|Expected):\s*(\d{1,10})\b", message)[:8]],
                    })
                row["failure_details"][case] = details
        persist()
        assert len(names) == 2 and set(names) == CASES
        assert all(n.find("skipped") is None and n.find("error") is None and
                   n.get("status") != "notrun" and n.get("result") != "suppressed" for n in nodes)
        if name == "stock":
            row["mismatches"] = {}
        for node, case in zip(nodes, names):
            failures = node.findall("failure")
            state = "FAILED" if name == "stock" else "OK"
            assert re.search(r"\[\s*" + state + r"\s*\]\s+" + re.escape(case) + r"(?=\s|$)", log)
            if name == "stock":
                assert len(failures) == 1
                message = failures[0].get("message") or "".join(failures[0].itertext())
                assert re.search(r"\bmtu\b", message)
                assert row["failure_details"][case][0]["numbers"] == [1220, 1234]
                row["mismatches"][case] = {"observed": 1220, "expected": 1234}
            else:
                assert not failures
    elif owner == UNIT:
        passed = set(re.findall(r"^\s*--- PASS: (Test[\w/=]+) \(", log, re.M))
        expected = {"TestNetworkMTU", "TestMTU", *(f"TestMTU/MaxFrameSize={n}" for n in (0,13,14,15,1500))}
        row["cases"] = sorted(passed & expected)
        assert passed >= expected and "FAIL:" not in log and "--- SKIP:" not in log
    else:
        # The upstream sh_binary has no gtest XML cases; verify its own shell trace and both readbacks.
        row["readbacks"] = sorted({int(s) for s in re.findall(r"^\s*\d+: test_veth01(?:@[^:]+)?: .*?\bmtu (1500|3000)\b", log, re.M)})
        row["finished_mac_case"] = "1a:2a:3a:4a:5a:6a" in log and "ip link del name test_veth01" in log
        assert row["readbacks"] == [1500,3000] and row["finished_mac_case"] and "SKIP:" not in log
    if name == "stock":
        assert row["native_exit"] == 3 and test["status"] == "FAILED"
    else:
        assert row["native_exit"] == 0 and test["status"] == "PASSED"
    row["disposition"] = "EXPECTED_FAILURE" if name == "stock" else "PASS"
    R["observed"].append(name)
    persist()


R = {"status":"INCOMPLETE", "stock_integration":STOCK, "fixed":FIXED, "base":BASE, "carrier":CARRIER,
     "expected":list(ARMS), "observed":[], "phases":{}, "errors":{}, "started_at":now(),
     "prior_wire_run":41856957832, "prior_artifact":13339106163}
persist()
assert git("rev-parse","HEAD") == CARRIER and git("rev-parse",CARRIER+"^") == STOCK
assert git("rev-parse",STOCK+"^") == FIXED and git("rev-parse",FIXED+"^") == BASE
assert git("diff","--name-only",BASE,STOCK).splitlines() == list(SOURCES)
assert git("diff","--name-only",PREVIOUS,FIXED).splitlines() == [SOURCES[1]]
assert not git("diff","--name-only",STOCK,FIXED,"--",*SOURCES)
for name, (source, owner) in ARMS.items():
    try:
        execute(name, source, owner)
    except Exception as exc:
        import traceback
        R["errors"][name] = {"type":type(exc).__name__, "lines":[v.lineno for v in traceback.extract_tb(exc.__traceback__) if Path(v.filename).name == "veth_mtu.py"]}
    persist()
R["status"] = "PASS" if not R["missing"] and not R["errors"] else "FAIL"
persist()
assert R["status"] == "PASS"
