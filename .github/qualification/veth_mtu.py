"""Recover the first operation or MTU readback from the existing fixed shell test."""

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
FIXED = "679d20c1ba36918979ce60e0f7619c3c72738d03"
STOCK = "cc65110cb1a021ebc7dd5526fb8334dca9fc6b2f"
BASE = "6dcadf4a84a07f69e8eb5f1ea9d7b2903bd03bd7"
PREVIOUS = "224d478ba5a85e1146267a60e2e7300e92c0b990"
ORIGINAL = "f1ccfd3be89aa530ae9d5cfae6f6673a891f0102"
SHELL = "//test/rtnetlink:setlink_test_"
SOURCES = ("test/rtnetlink/linux/setlink_test.sh", "test/syscalls/linux/socket_netlink_route.cc")
SCOPED = ["--config=x86_64", "--lockfile_mode=off", "--jobs=4", "--local_resources=memory=8192",
          "--remote_cache=", "--remote_executor=", "--bes_backend=",
          "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox"]
TEST = ["--strategy=TestRunner=local", "--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1",
        "--test_sharding_strategy=disabled", "--nozip_undeclared_test_outputs", "--test_timeout=150", "--test_output=errors"]
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
    return {"signals": [s for s in CLUES if s in joined],
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


ARMS = {"fixed_shell": (FIXED, SHELL + "runsc_systrap_overlay_directfs")}

# First occurrence of each statement in the *original* shell xtrace. No raw
# command, substituted path, arguments, or environment leave the worker.
TRACE = (
    ("source_helper", r"source .*rtnetlink_test\.sh"),
    ("lookup_mount", r"which mount"),
    ("copy_mount", r"cp \S+ /tmp/mount"),
    ("prepare_var_run", r"mkdir -p /var/run"),
    ("mount_tmpfs", r"(?:\S*/)?mount -t tmpfs test /var/run"),
    ("create_pair", r"ip link add name test_veth01 type veth peer name test_veth02"),
    ("read_link", r"ip link show test_veth01"),
    ("require_1500", r"grep -w ['\"]?mtu 1500"),
    ("set_3000", r"ip link set test_veth01 mtu 3000"),
    ("require_3000", r"grep -w ['\"]?mtu 3000"),
    ("delete_pair", r"ip link del name test_veth01"),
    ("rename_link", r"ip link set test_veth01 name test_veth03"),
    ("set_mac", r"ip link set dev test_veth01 address 1a:2a:3a:4a:5a:6a"),
)
# "setting up network:" is the caller's error context; the informational
# "Setting up network" at sandbox/network.go:55 is deliberately separate.
PHRASES = ("running container:", "starting container:", "creating sandbox:", "creating sandbox process",
           "setting up network:", "creating default loopback interface", "creating loopback link and routes",
           "urpc method", "containermanager.setnetworkargs", "connection reset by peer",
           "the sandbox exited before the container started", "starting root container:",
           "starting sub-container:", "creating container:", "mounting rootfs", "executing binary",
           "could not create", "failed to create", "failed to start", "error executing",
           "failed to mount", "mount source", "failed to connect", "failed to load", "exec format error",
           "permission denied", "operation not permitted", "operation not supported",
           "invalid argument", "no such file or directory", "file exists", "device or resource busy",
           "no such device", "not implemented", "command not found", "not found", "not supported",
           "not a directory", "read-only file system", "rtnetlink answers:", "segmentation fault",
           "panic:", "fatal error:", "signal: killed", "signal: bad system call",
           "sandbox process exited", "test failed with status:", "failed with error exit status")
FILES = ("sandbox.go", "network.go", "loader.go", "container.go", "boot.go", "gofer.go", "main.go",
         "setlink_test.sh", "rtnetlink_test.sh", "setup_container.go", "systrap.go", "overlay.go")


def projection(text):
    steps, first_errors, last_errors = [], [], []
    for n, line in enumerate(text.splitlines(), 1):
        m = re.search(r"(?:^|\s)\++\s+(.+)", line)
        if m:
            for key, pattern in TRACE:
                if re.match(pattern, m[1]) and len(steps) < 45:
                    steps.append([n, key])
                    break
        low = line.lower()
        terms = [s for s in PHRASES if s in low]
        if terms:
            locations = [[filename, int(pos)] for filename in FILES
                         for pos in re.findall(re.escape(filename) + r":(\d{1,5})\b", line)[:1]]
            operations = [k for k in ("netlink", "sentry", "gofer", "systrap", "seccomp", "loopback", "overlay",
                                      "tmpfs", "openat", "socketpair", "socket", "clone", "unshare", "setns", "bind",
                                      "connect", "sendmsg", "recvmsg", "ioctl", "pivot_root", "execve", "mount",
                                      "ptrace", "mmap", "setresuid", "setresgid", "sigsys", "sigkill", "bash", "ip")
                          if re.search(r"\b" + k + r"\b", low)]
            item = {"line":n, "terms":terms[:10], "locations":locations[:4], "operations":operations[:8]}
            if len(first_errors) < 4:
                first_errors.append(item)
            else:
                last_errors = (last_errors + [item])[-3:]
    return {"trace":steps, "errors":first_errors+last_errors, "network_info_seen":"Setting up network" in text,
            "link_mtu": [int(v) for v in re.findall(r"^\s*\d+: test_veth01(?:@[^:]+)?: .*?\bmtu (\d{1,5})\b", text, re.M)[:12]]}


def sandbox_logs(owner):
    # The authenticated TestResult is checked by read_attempt(). Follow up only
    # inside that owner's predeclared, canonical local output directory. Never
    # follow a symlink in the output tree, open FIFOs, or publish a log line.
    cache = (Path.home()/".cache/bazel").resolve(strict=True)
    base = Path("bazel-testlogs").resolve(strict=True)
    assert base.is_relative_to(cache) and base.name == "testlogs"
    directory = base / owner[2:].replace(":", "/") / "test.outputs"
    assert directory.resolve() == directory
    if not directory.is_dir():
        return {"directory":False, "members":[]}
    members = []
    for here, dirs, files in os.walk(directory, followlinks=False):
        here = Path(here)
        depth = len(here.relative_to(directory).parts)
        pattern = [r"[\w.-]{1,160}",r"runsc[0-9]+",r"logs",r"(?!)"][min(depth,3)]
        dirs[:] = sorted(d for d in dirs if re.fullmatch(pattern,d) and not (here/d).is_symlink())[:4]
        if depth != 3:
            continue
        for name in sorted(files):
            match = re.fullmatch(r"runsc\.log\.[\w:.+-]+\.(boot|gofer|run|start|create)\.txt",name)
            file = here / name
            if not match or file.is_symlink() or not file.is_file() or len(members) >= 6:
                continue
            size = file.stat().st_size
            with file.open("rb") as stream:
                head = stream.read(1024 * 1024)
                if len(head) < size:
                    stream.seek(max(len(head), size - 262144))
                    head += b"\n" + stream.read(262144)
            members.append({"command":match[1], "bytes":size, "complete":size <= 1024 * 1024,
                            "sample_sha256":hashlib.sha256(head).hexdigest(), **projection(head.decode(errors="replace"))})
    return {"directory":True, "members":members}


def execute(name, source, owner):
    checkout(source)
    row = R["phases"][name] = {"source":source, "target":owner}
    events = bazel(row, name, ["test", *SCOPED, *TEST, owner])
    log, _ = read_attempt(row, events, owner, source)
    row["original_log"] = projection(log)
    row["mac_readback"] = bool(re.search(r"^\s*link/ether 1a:2a:3a:4a:5a:6a\b", log, re.M))
    row["skip_seen"] = "SKIP:" in log
    persist()
    row["sandbox_logs"] = sandbox_logs(owner)
    persist()
    test = row["test"]
    assert not test["cached"] and test["strategy"] == "local"
    steps = [s for _, s in row["original_log"]["trace"]]
    assert row["native_exit"] == 0 and test["status"] == "PASSED"
    assert {"create_pair","set_3000","set_mac","delete_pair"} <= set(steps)
    assert set(row["original_log"]["link_mtu"]) == {1500,3000} and row["mac_readback"] and not row["skip_seen"]
    row["disposition"] = "PASS"
    R["observed"].append(name)
    persist()

R = {"status":"INCOMPLETE", "stock_integration":STOCK, "fixed":FIXED, "base":BASE, "carrier":CARRIER,
     "expected":list(ARMS), "observed":[], "phases":{}, "errors":{}, "started_at":now(),
     "previous_shell":{"run":41860421336,"artifact":13339549679,
                       "zip_sha256":"2a8e64a4c6cc2d1a632db665a03cfb8b84d22fda03c997609073745975a2c368",
                       "log_sha256":"6dc84c27d20b467b74cb609052b3ff6bb812980efb5ef0ae001a11f3a48b1068",
                       "native_exit":3,"status":"FAILED","mtu_readbacks":[]},
     "prior_wire_run":41856957832, "prior_artifact":13339106163}
persist()
assert git("rev-parse","HEAD") == CARRIER and git("rev-parse",CARRIER+"^") == ORIGINAL and git("rev-parse",ORIGINAL+"^") == STOCK
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
