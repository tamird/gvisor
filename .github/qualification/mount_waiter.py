"""Qualify the registered-waiter ownership contract in the VFS package."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlparse
import re
import xml.etree.ElementTree as ET
import subprocess

ROOT = Path(os.environ["RUNNER_TEMP"]).resolve()
PUBLIC = ROOT / "out" / "verdict.json"
CARRIER = os.environ["GITHUB_SHA"]
FIXED = "f11e71355d869135bab5bdadb03d2a9f9ee65eab"
CONTROL = "4ce97bb87e6822b1cc241588bc02f0094562a935"
PUBLIC_HEAD = "6fe5701556558d631f24919814f014172204208d"
ORIGINAL = "509ad5337346f8cbdba515cefadd542de28d6156"
UNIT = "//pkg/sentry/vfs:vfs_test"
CASE = "TestMountPromiseResolvedAfterRegister"
SOURCE = "pkg/sentry/vfs/mount_promise.go"
FIXTURE = "pkg/sentry/vfs/mount_test.go"
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
    locations = re.findall(r"\b(mount_test\.go|mount_promise\.go|mount\.go|resolving_path\.go)\D{1,12}(\d{1,5})", joined)
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


def execute(name, source):
    checkout(source)
    row = R["phases"][name] = {"source":source, "target":UNIT}
    filtered = name == "control"
    args = ["test", *SCOPED, *TEST, "--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1"]
    if filtered:
        args.append("--test_arg=-test.run=^" + CASE + "$")
    events = bazel(row, name, args + [UNIT])
    log, outputs = read_attempt(row, events, UNIT, source)
    xml = test_output(outputs["test.xml"], UNIT, "test.xml")
    row["xml_sha256"] = hashlib.sha256(xml).hexdigest()
    cases = []
    for case in ET.fromstring(xml).iter("testcase"):
        failed = case.find("failure") is not None or case.find("error") is not None
        skipped = case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "suppressed"
        cases.append((case.get("name"), "FAIL" if failed else "SKIP" if skipped else "PASS"))
    row["xml_counts"] = {s:sum(v == s for _, v in cases) for s in ("PASS","FAIL","SKIP")}
    row["named_xml"] = [v for test,v in cases if test == CASE]
    log = log.replace("\x16", "")
    row["cases_run"] = re.findall(r"^=== RUN\s+(Test\w+)\s*$", log, re.M)
    row["named_outcomes"] = re.findall(r"^\s*--- (PASS|FAIL|SKIP): " + CASE + r" \(", log, re.M)
    row["closed_waiter_lines"] = [int(m) for m in re.findall(r"mount_test\.go:(\d+): lookup closed a registered waiter channel", log)]
    row["wrong_mount_or_root"] = bool(re.search(r"mount_test\.go:\d+: rp\.(Mount|Start)\(\) =",log))
    persist()
    assert row["test"]["strategy"] == "local" and not row["test"]["cached"]
    assert row["named_xml"] == row["named_outcomes"] and not row["wrong_mount_or_root"]
    if filtered:
        assert row["native_exit"] == 3 and row["test"]["status"] == "FAILED"
        assert row["cases_run"] == [CASE] and row["named_outcomes"] == ["FAIL"]
        assert len(cases) == len(row["closed_waiter_lines"]) == 1
        row["disposition"] = "EXPECTED_CLOSED_WAITER"
    else:
        assert row["native_exit"] == 0 and row["test"]["status"] == "PASSED"
        assert row["named_outcomes"] == ["PASS"] and not row["closed_waiter_lines"]
        assert all(v == "PASS" for _, v in cases)
        paths = ["pkg/sentry/vfs/" + f + "_test.go" for f in ("dentry","device","file_description_impl_util","mount","posix_acl")]
        expected = re.findall(r"^func (Test\w+)\(", git("grep","-h","^func Test","HEAD","--",*paths), re.M)
        assert CASE in expected and len(expected) == 14 and sorted(expected) == sorted(row["cases_run"])
        row["disposition"] = "PASS"
    R["observed"].append(name)
    persist()


R = {"status":"INCOMPLETE", "control":CONTROL, "fixed":FIXED, "public_head":PUBLIC_HEAD,
     "original":ORIGINAL, "carrier":CARRIER, "expected":["control","fixed"], "observed":[],
     "phases":{}, "errors":{}, "started_at":now()}
persist()
assert git("rev-parse","HEAD") == CARRIER and git("rev-parse",CARRIER+"^") == CONTROL
assert git("rev-parse",CONTROL+"^") == FIXED and git("rev-parse",FIXED+"^") == PUBLIC_HEAD
assert git("rev-parse",PUBLIC_HEAD+"^") == ORIGINAL
assert git("rev-parse",FIXED+"^{tree}") == "3a51d3304c9a8fbffbe522f5e2afcade0e1ead72"
assert git("diff","--name-only",FIXED,CONTROL) == SOURCE
assert git("rev-parse",FIXED+":"+FIXTURE) == git("rev-parse",CONTROL+":"+FIXTURE) == "ab90ab0e148988b9d0944fea823a5b91207cbe94"
old = git("show",ORIGINAL+":"+SOURCE)
control = git("show",CONTROL+":"+SOURCE)
cut = "\tdefer mp.wq.EventUnregister(&e)\n"
adapter = ("\treturn vfs.waitOnMountPromise(ctx, rp, mp, ch)\n}\n\n"
           "func (vfs *VirtualFilesystem) waitOnMountPromise(ctx context.Context, rp *ResolvingPath, mp *mountPromise, ch chan struct{}) error {\n"
           "\tvd := VirtualDentry{rp.mount, rp.start}\n")
assert old.count(cut) == 1 and old.replace(cut,cut + adapter) == control
R["control_adaptation"] = "historical 509ad5337 body with callable post-registration boundary and original bidirectional channel"
R["bazel"] = subprocess.check_output(["bazel","--version"],text=True).strip()
assert R["bazel"] == "bazel 8.8.1"
for name, source in (("control", CONTROL),("fixed", FIXED)):
    try:
        execute(name, source)
    except Exception as exc:
        import traceback
        R["errors"][name] = {"type":type(exc).__name__,"lines":[v.lineno for v in traceback.extract_tb(exc.__traceback__) if Path(v.filename).name == "mount_waiter.py"]}
    persist()
R["status"] = "PASS" if not R["missing"] and not R["errors"] else "FAIL"
persist()
assert R["status"] == "PASS"
