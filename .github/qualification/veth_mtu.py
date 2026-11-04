"""Build the original releases, then join fresh Bazel owners to the same wire fixture."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET
import re
import shutil
import subprocess
import sys

ROOT = Path(sys.argv[1]).resolve()
PUBLIC = ROOT / "public" / "result.json"
CARRIER = sys.argv[2]
FIXED = "224d478ba5a85e1146267a60e2e7300e92c0b990"
STOCK = "6dcadf4a84a07f69e8eb5f1ea9d7b2903bd03bd7"
GNU = "gcr.io/gvisor-presubmit/default_x86_64@sha256:509c3211494b2ab580b120454e6d9fe7f2a7e80fabd87c97100fdb99e3066e72"
WIRE = "//test/rtnetlink/linux:source_veth_mss"
UNIT = "//pkg/tcpip/link/ethernet:ethernet_test"
FILES = {"runsc", "containerd-shim-runsc-v1", *("gvisor-bin/" + s for s in (
    "checkpointgofer", "gvisor_sentry", "runsc-metric-server", "runsc-netgofer",
    "runsc-fd-parking", "gvisor-sentry-prewarmer", "gvisor-proc-umounter"))}
SCOPED = ["--jobs=4", "--local_resources=memory=8192", "--remote_cache=", "--remote_executor=",
          "--bes_backend=", "--spawn_strategy=local", "--strategy=GoStdlib=processwrapper-sandbox",
          "--strategy=GoStdlibList=processwrapper-sandbox", "--verbose_failures"]
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
         "not a known option", "not executable", "segmentation fault", "killed")


def now():
    return datetime.now(timezone.utc).isoformat()


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def persist():
    R["missing"] = [k for k in R["expected"] if k not in R["observed"]]
    R["verified_at"] = now()
    PUBLIC.write_text(json.dumps(R, sort_keys=True, indent=2) + "\n")


def selected(buf):
    lines = buf.decode(errors="replace").splitlines()
    joined = "\n".join(lines).lower()
    locations = re.findall(r"\b(veth_wire\.py|packaged_bridge\.py|ethernet(?:_test)?\.go|vdso\.cc|vdso_time\.cc)\D{1,12}(\d{1,5})", joined)
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


def bazel(row, label, args, host=False):
    row["started_at"] = now()
    bep = ROOT / (label + ".bep")
    argv = [str(ROOT / "bin/bazel")] if host else ["docker", "exec", "veth-builder", "/usr/local/bin/bazel"]
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
    assert label in (WIRE, UNIT) and name in ("test.log", "test.xml")
    assert item["name"] == name and (label == UNIT or name == "test.log")
    owner = label[2:].replace(":", "/")
    parsed = urlparse(item["uri"])
    assert parsed.scheme == "file" and not (parsed.netloc or parsed.query or parsed.fragment or parsed.params)
    path = Path(unquote(parsed.path))
    assert path.is_absolute() and path.parts[-len(Path(owner).parts)-2:] == ("testlogs", *Path(owner).parts, name)
    prefix = [] if label == WIRE else ["docker", "exec", "veth-builder"]
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
    return subprocess.check_output([*prefix, "bash", "-eu", "-c", check, "read-test-output", owner, name, str(path)])


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


def build(side, ref):
    k = side + "_release"
    row = R["phases"][k] = {"source":ref, "owner":"//:release"}
    checkout(ref)
    bazel(row, k, ["build", "--config=x86_64", "--compilation_mode=opt", *SCOPED, "//:release"])
    if row["native_exit"]:
        return False
    release = ROOT / (side + "-release")
    subprocess.run(["docker","exec","veth-builder","cp","-RL","bazel-bin/release",str(release)], check=True)
    files = {p.relative_to(release).as_posix():sha(p) for p in release.rglob("*") if p.is_file()}
    assert set(files) == FILES and all(os.access(release / name, os.X_OK) for name in files)
    row["files"] = files
    row["machines"] = {}
    for name in ("runsc", "gvisor-bin/gvisor_sentry"):
        with (release / name).open("rb") as stream:
            header = stream.read(20)
        assert header[:6] == b"\x7fELF\x02\x01"
        row["machines"][name] = int.from_bytes(header[18:20], "little")
        assert row["machines"][name] == 62
    # Stage only the exact declared, byte-checked products as Bazel data for the fixture.
    checkout(CARRIER)
    staged = Path("test/rtnetlink/linux/veth-release")
    if staged.exists():
        assert not staged.is_symlink() and {p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file()} == FILES
        shutil.rmtree(staged)
    shutil.copytree(release, staged)
    assert all(sha(staged / name) == value for name, value in files.items())
    R["observed"].append(k)
    persist()
    return True


def wire(side):
    checkout(CARRIER)
    row = R["phases"][side] = {"fixture":R["fixture"], "runtime":side}
    events = bazel(row, side, ["test", *SCOPED, *TEST, "--test_env=VETH_MODE="+side, WIRE], host=True)
    log, _ = read_attempt(row, events, WIRE, CARRIER)
    observations = []
    shapes = {"loopback":{"initial","requested","observed"}, "restored_loopback":{"initial","observed"},
              "transfer":{"configured","local","peer","port","exchange","mss","timestamps"}}
    for raw in re.findall(r"^VETH_WIRE (\{[^\n]+\})$", log, re.M):
        data = json.loads(raw)
        label = data.pop("phase")
        assert label in shapes and set(data) == shapes[label]
        assert all(type(n) is bool if k in ("exchange","timestamps") else type(n) is int and 0 <= n <= 65536 for k,n in data.items())
        observations.append({"phase":label, **data})
    row["observations"] = observations
    row["observed_fixtures"] = re.findall(r"^VETH_FIXTURE ([a-f0-9]{64})$",log,re.M)
    row["launched"] = dict(re.findall(r"^VETH_BINARY (runsc|gvisor_sentry) ([a-f0-9]{64})$",log,re.M))
    persist()
    assert row["native_exit"] == 0 and row["test"]["status"] == "PASSED" and not row["test"]["cached"] and row["test"]["strategy"] in ("local","standalone")
    assert row["observed_fixtures"] == [R["fixture"]]
    if side != "linux":
        files = R["phases"][side+"_release"]["files"]
        assert row["launched"] == {"runsc":files["runsc"],"gvisor_sentry":files["gvisor-bin/gvisor_sentry"]}
    delta = 14 if side == "stock" else 0
    assert len(row["observations"]) == 4
    loop, restored, *transfers = row["observations"]
    assert loop == {"phase":"loopback","initial":65536-delta,"requested":1234,"observed":1234-delta}
    assert restored == {"phase":"restored_loopback","initial":loop["initial"],"observed":loop["initial"]}
    for i, (obs, expected) in enumerate(zip(transfers, (1500, 3000), strict=True)):
        assert obs["phase"] == "transfer" and obs["configured"] == expected and obs["local"] == obs["peer"] == expected-delta
        assert obs["mss"] == expected-40-delta and obs["exchange"] is True and obs["port"] == 30149+i and isinstance(obs["timestamps"], bool)
    R["observed"].append(side)
    persist()


def unit():
    checkout(FIXED)
    row = R["phases"]["unit"] = {"source":FIXED}
    events = bazel(row, "unit", ["test","--config=x86_64","--compilation_mode=opt",*SCOPED,*TEST,UNIT])
    log, outputs = read_attempt(row, events, UNIT, FIXED)
    xml = test_output(outputs["test.xml"], UNIT, "test.xml")
    cases = ET.fromstring(xml).findall(".//testcase")
    row["cases"] = [c.get("name") for c in cases]
    row["xml_sha256"] = hashlib.sha256(xml).hexdigest()
    assert row["native_exit"] == 0 and row["test"]["status"] == "PASSED" and not row["test"]["cached"] and row["test"]["strategy"] in ("local","standalone")
    assert any(n == "TestNetworkMTU" for n in row["cases"]) and any(n.startswith("TestMTU") for n in row["cases"])
    assert not any(c.find("failure") is not None or c.find("error") is not None or c.find("skipped") is not None for c in cases)
    assert all(c.get("status") != "notrun" and c.get("result") != "suppressed" for c in cases)
    R["observed"].append("unit")
    persist()


R = {"status":"INCOMPLETE", "stock":STOCK, "fixed":FIXED, "carrier":CARRIER, "gnu":GNU,
     "expected":["linux","stock_release","stock","fixed_release","fixed","unit"], "observed":[], "phases":{},
     "fixture":sha(Path("test/rtnetlink/linux/veth_wire.py")), "errors":{}, "started_at":now()}
persist()
assert git("rev-parse","HEAD") == CARRIER and git("rev-parse",CARRIER+"^") == FIXED and git("rev-parse",FIXED+"^") == STOCK
for phase, fn in (("linux", lambda:wire("linux")), ("stock_release", lambda:build("stock",STOCK)),
                  ("stock",lambda:wire("stock")), ("fixed_release",lambda:build("fixed",FIXED)),
                  ("fixed",lambda:wire("fixed")), ("unit",unit)):
    if phase in ("stock","fixed") and phase+"_release" not in R["observed"]:
        continue
    if phase == "fixed_release" and "stock_release" not in R["observed"]:
        continue  # Do not repeat a common compiler prerequisite failure on a second source.
    try:
        fn()
    except Exception as e:
        R["errors"][phase] = {"type":type(e).__name__,"source_line":e.__traceback__.tb_lineno}
        import traceback
        R["errors"][phase]["lines"] = [v.lineno for v in traceback.extract_tb(e.__traceback__) if Path(v.filename).name == "veth_mtu.py"]
    persist()
R["status"] = "PASS" if not R["missing"] and not R["errors"] else "FAIL"
persist()
assert R["status"] == "PASS"
