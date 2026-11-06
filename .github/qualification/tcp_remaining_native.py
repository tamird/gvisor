"""Record each missing outcome from the ordinary local Bazel test runner."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

ROOT = Path(os.environ["RUNNER_TEMP"]) / "typed-shutdown"
SOURCE = "6e1f1e7005d85fa0e2f04d75c530abf1ba384490"
SYSCALL = "//test/syscalls:tcp_socket_test_runsc_ptrace"
ANALYSIS = [
    "//pkg/tcpip/transport/tcp/test/e2e:" + name + "_nogo"
    for name in ("tcp_test", "tcp_rack_test", "tcp_sack_test", "tcp_noracedetector_test")
]
EXPECTED = {
    f"AllInetTests/TcpSocketTest.{name}/{i}"
    for name in ("HalfCloseReadinessLocal", "HalfCloseReadinessPeer", "PollAfterShutdown")
    for i in range(2)
}
report = {"source": SOURCE, "phases": {}}


def save():
    (ROOT / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def run(name, labels, options, minutes):
    bep = ROOT / (name + ".bep")
    console = ROOT / (name + ".log")
    phase = {"labels": {label: {"attempts": 0} for label in labels}, "verified": False}
    report["phases"][name] = phase
    save()
    with console.open("w") as out:
        phase["exit"] = subprocess.run([
            "timeout", "-k", "20", str(minutes*60), str(ROOT / "bazel"),
            "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx2600m", "test",
            "--config=x86_64", "--jobs=2", "--local_resources=memory=9900", "--keep_going",
            "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
            "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
            "--spawn_strategy=linux-sandbox", "--strategy=TestRunner=local", "--remote_executor=",
            "--remote_cache=", "--bes_backend=", "--test_tag_filters=", "--build_tag_filters=",
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_sharding_strategy=disabled",
            "--test_timeout=450", "--color=no", "--curses=no", "--noshow_progress", "--test_output=errors",
            "--build_event_json_file="+str(bep), *options, *labels,
        ], stdout=out, stderr=subprocess.STDOUT).returncode
    save()
    if bep.exists():
        with bep.open() as events:
            for line in events:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if "started" in event:
                    phase["invocation"] = event["started"].get("uuid")
                label = event["id"].get("testResult", {}).get("label")
                attempt = event.get("testResult")
                if label not in labels or not attempt:
                    continue
                rec = phase["labels"][label]
                rec["attempts"] += 1
                info = attempt.get("executionInfo", {})
                rec.update(status=attempt.get("status"), strategy=info.get("strategy"),
                           cached=bool(attempt.get("cachedLocally") or info.get("cachedRemotely")))
                for entry in attempt.get("testActionOutput", []):
                    filename = entry["name"]
                    if filename not in ("test.log", "test.xml"):
                        continue
                    uri = urlsplit(entry.get("uri", ""))
                    if uri.scheme != "file":
                        continue
                    path = Path(unquote(uri.path))
                    if not path.is_file():
                        continue
                    data = path.read_bytes()
                    rec[filename+"_sha256"] = hashlib.sha256(data).hexdigest()
                    if filename == "test.xml" and label == SYSCALL:
                        try:
                            nodes = ET.fromstring(data).iter("testcase")
                        except ET.ParseError:
                            continue
                        actual = {}
                        for node in nodes:
                            case = node.get("classname", "")+"."+node.get("name", "")
                            if case in EXPECTED:
                                actual[case] = not any(node.find(tag) is not None for tag in ("skipped", "error", "failure")) and node.get("status") != "notrun" and node.get("result") != "suppressed"
                        rec["xml"] = actual
                    if filename == "test.log":
                        text = data.decode(errors="replace")
                        if label == SYSCALL:
                            rec["gtest"] = {case: status for status, case in re.findall(r"\[\s*(OK|SKIPPED|FAILED)\s*\]\s+(AllInetTests/TcpSocketTest\.\w+/\d+)\s+\(", text) if case in EXPECTED}
                        else:
                            parts = re.split(r"(?m)^(amd64|arm64)\.\.\.", text)
                            rec["architectures"] = {arch: body.strip() == "PASS" for arch, body in zip(parts[1::2], parts[2::2])}
    checks = []
    for label, rec in phase["labels"].items():
        fresh = rec["attempts"] == 1 and rec.get("strategy") == "local" and not rec.get("cached") and rec.get("status") == "PASSED"
        if label == SYSCALL:
            xml, log = rec.get("xml", {}), rec.get("gtest", {})
            names_passed = xml == dict.fromkeys(EXPECTED, True) or log == dict.fromkeys(EXPECTED, "OK")
            rec["verified"] = bool(fresh and names_passed and all(xml.values()) and all(v == "OK" for v in log.values()))
        else:
            rec["verified"] = bool(fresh and rec.get("architectures") == {"amd64": True, "arm64": True})
        checks.append(rec["verified"])
    phase["verified"] = phase["exit"] == 0 and all(checks)
    if not phase["verified"]:
        first = []
        with console.open(errors="replace") as stream:
            for line in stream:
                if re.search(r"^ERROR:|fatal error:|panic:|permission denied|unexpected call|killed|no such file or directory|not permitted|undefined reference", line, re.I) and len(first)<10:
                    if not re.search(r"https?://|token|credential|bearer|api[_-]?key", line, re.I):
                        first.append(line.strip()[:360])
        phase["diagnostics"] = first
    save()
    print(name, phase["exit"], phase["verified"], flush=True)


assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
subprocess.run(["git", "diff", "--exit-code", "HEAD"], check=True)
run("syscalls", [SYSCALL], ["--test_env=GTEST_FILTER="+":".join(sorted(EXPECTED))], 65)
run("analysis", ANALYSIS, [], 64)
report["verified"] = all(p["verified"] for p in report["phases"].values())
save()
raise SystemExit(0 if report["verified"] else 1)
