"""Qualify the four local futex syscall modes and the native Nogo runner independently."""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from xml.etree import ElementTree

import native
import previous


source = "b9a1f67367bee54b536a3cc56a3cffec9629bf13"
group, = sys.argv[1:]
assert group in ("syscalls", "nogo")
assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
assert os.uname().machine == "x86_64" and os.geteuid() != 0
cases = ["SharedPrivate/PrivateAndSharedFutexTest.RequeueCountsWaiters/" + str(i) for i in (0, 1)]
nogo = "//pkg/sentry/kernel/futex:futex_nogo"
modes = ["native", "runsc_systrap_directfs", "runsc_systrap_directfs_save", "runsc_systrap_directfs_save_resume"]
targets = [nogo] if group == "nogo" else ["//test/syscalls:futex_test_" + mode for mode in modes]
out = native.OUT / group
out.mkdir(parents=True, exist_ok=True)
report = {"source": source, "kernel": os.uname().release, "targets": targets, "attempts": [], "tests": {}}
previous.OUT = native.WORK
previous.assets.DIST.mkdir(parents=True, exist_ok=True)


def save():
    (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def test(event, label, identity):
    result = event["testResult"]
    strategy = result.get("executionInfo", {})
    row = report["tests"][label] = {
        "status": result["status"], "strategy": strategy.get("strategy"),
        "cached": bool(result.get("cachedLocally") or strategy.get("cachedRemotely")),
        "attempt": identity.get("attempt", 1), "shard": identity.get("shard", 1), "verified": False,
    }
    files = {}
    for f in result.get("testActionOutput", []):
        name = f["name"]
        if name in ("test.log", "test.xml"):
            _, data = native.contents(f, 28 << 20, "/testlogs/" + label[2:].replace(":", "/") + "/" + name)
            row[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            files[name] = data
    save()
    log = files["test.log"].decode(errors="replace")
    if label == nogo:
        row["analyzed"] = sorted(set(re.findall(r"(amd64|arm64)\.\.\.", log)))
        matched = row["analyzed"] == ["amd64", "arm64"]
    else:
        suite = ElementTree.fromstring(files["test.xml"])
        elements = list(suite.iter("testcase"))
        row["xmlCases"] = {}
        for element in elements:
            name = element.get("classname", "") + "." + element.get("name", "")
            if name in cases:
                row["xmlCases"][name] = all(element.find(key) is None for key in ("skipped", "failure", "error")) and element.get("status") != "notrun" and element.get("result") not in ("skipped", "suppressed")
        row["started"] = [case for case in cases if re.search(r"(?m)^\[ RUN\s+\] " + re.escape(case) + r"\s*$", log)]
        row["passed"] = [case for case in cases if re.search(r"(?m)^\[\s+OK\s*\] " + re.escape(case) + r"(?:\s|$)", log)]
        clean = ElementTree.Element("testsuite", name=label, source_sha256=row["test.xml"]["sha256"])
        for name, ok in row["xmlCases"].items():
            case = ElementTree.SubElement(clean, "testcase", name=name)
            if not ok:
                ElementTree.SubElement(case, "failure")
        ElementTree.ElementTree(clean).write(out / (label.split(":")[1] + ".xml"), encoding="utf-8", xml_declaration=True)
        matched = (len(elements) == 2 and all(row["xmlCases"].get(n) for n in cases) and
                   row["started"] == row["passed"] == cases)
    row["verified"] = (matched and row["strategy"] in ("local", "standalone") and row["status"] == "PASSED" and
                       not row["cached"] and row["attempt"] == row["shard"] == 1)
    save()


def scan(bep, attempt):
    if not bep.exists():
        return
    with bep.open() as stream:
        for line in stream:
            if not line.endswith("\n"):
                continue
            event = json.loads(line)
            if "started" in event:
                attempt["invocation"] = event["started"]["uuid"]
            if "finished" in event:
                attempt["exitName"] = event["finished"].get("exitCode", {}).get("name")
            if "action" in event and not event["action"].get("success") and len(attempt["failedActions"]) < 10:
                action = event["action"]
                ident = event["id"].get("actionCompleted", {})
                attempt["failedActions"].append([ident.get("label"), action.get("type"), action.get("exitCode"),
                    {k:v.get("code") for k,v in action.get("failureDetail", {}).items() if isinstance(v, dict)}])
            if "testResult" in event:
                ident = event["id"]["testResult"]
                if ident["label"] in targets:
                    try:
                        test(event, ident["label"], ident)
                    except (AssertionError, KeyError, ValueError, OSError) as err:
                        report["tests"][ident["label"]]["readError"] = type(err).__name__
                        save()


save()
end = time.monotonic() + (75 if group == "syscalls" else 55) * 60
for i in range(20):
    remaining = [t for t in targets if t not in report["tests"]]
    if not remaining:
        break
    bep = native.WORK / (group + "-" + str(i) + ".bep")
    console = native.WORK / (group + "-" + str(i) + ".console")
    cmd = [str(native.WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g", "test",
           *native.FLAGS, "--build_event_json_file=" + str(bep),
           *["--distdir=" + str(d) for d in sorted(previous.DIRS)]]
    if group == "syscalls":
        cmd.append("--test_env=GTEST_FILTER=" + ":".join(cases))
    else:
        cmd.append("--jobs=2")
    with console.open("w+b") as log:
        try:
            rc = subprocess.run(cmd + remaining, stdout=log, stderr=subprocess.STDOUT, timeout=max(1, end - time.monotonic())).returncode
        except subprocess.TimeoutExpired:
            rc = 124
        log.seek(0)
        text = log.read().decode(errors="replace")
    attempt = {"exit": rc, "targets": remaining, "failedActions": []}
    attempt["diagnostics"] = sorted(set(re.findall(r"(?i)out of memory|killed signal|no space left on device|input/output error|Error downloading|no such package|fatal error: [A-Za-z0-9_./+-]+: No such file or directory|Permission denied", text)))[:12]
    report["attempts"].append(attempt)
    save()
    scan(bep, attempt)
    save()
    if rc in (0, 3) or time.monotonic() >= end or not previous.recover(text):
        break
report["verified"] = all(report["tests"].get(t, {}).get("verified") for t in targets)
save()
raise SystemExit(0 if report["verified"] else 1)
