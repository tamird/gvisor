"""Verify the netgofer idle-slot boundary on the same native ARM64 Linux worker."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

import previous

BASE = "dd49ce763bea6f5b38eff62f2ca586b6a22c4a9f"
FIX = "844f481b1ba6b4f3cd512ed28ae0146d52c31197"
WORK = Path(os.environ["RUNNER_TEMP"]) / "netgofer-idle"
OUT = WORK / "receipt"
GO_TEST = Path("runsc/gofer/net/netgofer_test.go")
GO = Path("runsc/gofer/net/netgofer.go")
LABEL = "//runsc/gofer/net:netgofer_test"
TEST = "TestProxyUDPIdleTimeout"
REPORT = {"base": BASE, "candidate": FIX, "arch": platform.machine(), "verified": False,
          "comparison": {}, "assets": {}}
previous.OUT = OUT
previous.save = lambda: None
previous.REPORT["assets"] = REPORT["assets"]

def save():
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2) + "\n")

def run(name, source, *, count=1, suite=False, oversized=False):
    subprocess.run(["git", "checkout", "-q", "--detach", source], check=True)
    for p in (GO_TEST, GO):
        p.write_bytes(subprocess.check_output(["git", "show", source + ":" + str(p)]))
    if oversized:
        text = GO_TEST.read_text()
        before, test, rest = text.partition("func TestProxyUDPIdleTimeout(")
        body, next_func, after = rest.partition("\nfunc ")
        send = "if _, err := udpPC.WriteTo(buf[:nr], peer); err != nil {"
        assert test and next_func and body.count(send) == 1
        # The echo server sends an extra byte only on the second exchange.
        body = body.replace(send, 'if string(buf[:nr]) == "idle-timeout-2" { buf[nr] = 33; nr++ }; ' + send)
        GO_TEST.write_text(before + test + body + next_func + after)
    delta = subprocess.check_output(["git", "diff", "--", str(GO_TEST), str(GO)])
    if delta:
        (OUT / (name+".patch")).write_bytes(delta)
    args = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g", "test",
            "--config=aarch64", "--jobs=4", "--lockfile_mode=off", "--remote_executor=", "--remote_cache=",
            "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local",
            "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
            "--strategy=TestRunner=local", "--test_sharding_strategy=disabled", "--nocache_test_results",
            "--runs_per_test=1", "--flaky_test_attempts=1", "--build_tag_filters=", "--test_tag_filters=",
            "--test_timeout=300", "--test_output=errors", "--nobuild_event_json_file_path_conversion",
            "--test_arg=-test.v", f"--test_arg=-test.count={count}"]
    if not suite:
        args.append("--test_arg=-test.run=^" + TEST + "$")
    for attempt in range(10):
        bep = WORK / (name + ".bep")
        console = WORK / (name + ".console")
        with console.open("w") as f:
            proc = subprocess.run([*args, "--build_event_json_file=" + str(bep),
                                   *["--distdir=" + str(d) for d in sorted(previous.DIRS)], LABEL],
                                  stdout=f, stderr=subprocess.STDOUT, timeout=70*60)
        events = [json.loads(l) for l in bep.read_bytes().splitlines()] if bep.exists() else []
        results = [e for e in events if "testResult" in e and e["id"]["testResult"]["label"] == LABEL]
        if results:
            break
        log = console.read_text(errors="replace")
        print(name, "before test action", proc.returncode, flush=True)
        if not previous.recover(log):
            (OUT / (name + "-build.txt")).write_text("\n".join(l[:2000] for l in log.splitlines() if "ERROR:" in l or "Error downloading" in l)[-10000:])
            raise RuntimeError(name + " stopped before running the test")
    item, = results
    tr = item["testResult"]
    info = tr["executionInfo"]
    assert not tr.get("cachedLocally") and not info.get("cachedRemotely") and info.get("strategy") in {"local", "standalone"}, info
    for key in ["test.log", "test.xml"]:
        output, = [f for f in tr["testActionOutput"] if f.get("name") == key]
        u = urlsplit(output["uri"])
        assert u.scheme=="file" and not u.netloc
        data = Path(unquote(u.path)).read_bytes()
        assert len(data) < 2_000_000 and (not output.get("digest") or output["digest"] == hashlib.sha256(data).hexdigest())
        (OUT / (name+"."+key)).write_bytes(data)
    cases = list(ET.parse(OUT / (name+".test.xml")).iter("testcase"))
    log = (OUT / (name+".test.log")).read_text(errors="replace")
    assert any(c.get("name") == TEST and c.find("skipped") is None for c in cases)
    passes = len(re.findall(r"(?m)^\s*--- PASS: "+TEST+r" \(", log))
    failures = len(re.findall(r"(?m)^\s*--- FAIL: "+TEST+r" \(", log))
    rejects = len(re.findall("Rejecting UDP flow to .*max active connections",log))
    row = {"source":source, "xmlCases":len(cases), "xmlSkips":sum(c.find("skipped") is not None for c in cases),
           "passes":passes, "failures":failures, "admissionRejections":rejects, "bazelExit":proc.returncode,
           "status":tr["status"], "worker":info.get("strategy"), "attempt":item["id"]["testResult"].get("attempt",1),
           "invocation":next(e["started"]["uuid"] for e in events if "started" in e)}
    REPORT["comparison"][name] = row
    save()
    print(name, row, flush=True)
    if oversized:
        assert proc.returncode and failures == 1 and passes == 0, row
        assert 'conn2.Read() = "idle-timeout-2!", <nil>; want "idle-timeout-2"' in log
    else:
        assert proc.returncode == 0 and tr["status"] == "PASSED" and passes == count and failures == 0, row
        if not suite: assert rejects == 0, row
        if suite: assert len(cases) >= 20 and row["xmlSkips"] == 0, row
    subprocess.run(["git", "restore", "--",str(GO_TEST),str(GO)], check=True)

assert platform.machine() == "aarch64"
save()
run("full-owner", FIX, suite=True)
run("long-second-echo", FIX, oversized=True)
REPORT["verified"] = True
save()
