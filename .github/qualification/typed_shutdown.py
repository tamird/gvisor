"""Record the original named checks for the typed shutdown revision."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

PRODUCT = "bf20197d4c2ba3b36cbd9bb034f407a9c5cce5e6"
ROOT = Path(os.environ["RUNNER_TEMP"]) / "typed-shutdown"
TCP = "//pkg/tcpip/transport/tcp:"
E2E = "//pkg/tcpip/transport/tcp/test/e2e:"
ATOMIC = "//pkg/atomicbitops:atomicbitops"
OWNERS = [TCP + "tcp_test", ATOMIC + "_test"] + [E2E + n for n in (
    "tcp_test dual_stack_test forwarder_test rcv_test sack_scoreboard_test "
    "tcp_timestamp_test tcp_rack_test tcp_acktiming_test tcp_sack_test tcp_noracedetector_test").split()]
NOGO = [TCP + "tcp_nogo", E2E + "e2e_nogo", ATOMIC + "_nogo", *[label + "_nogo" for label in OWNERS]]
SYSCALL = "//test/syscalls:tcp_socket_test_runsc_ptrace"
GTEST = {f"AllInetTests/TcpSocketTest.{name}/{i}" for i in range(2) for name in (
    "HalfCloseReadinessLocal", "HalfCloseReadinessPeer", "PollAfterShutdown")}
report = {"source": PRODUCT, "cases": {}}


def save():
    (ROOT / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def diagnostic(line):
    if re.search(r"token|credential|api[-_]key|bearer|https?://", line, re.I):
        return None
    # Limit disclosure to generated or checked-in code locations and analyzer findings.
    match = re.search(r"(?:pkg/(?:tcpip/transport/tcp|atomicbitops)/[\w./-]+\.go:\d+(?::\d+)?[: ].{1,275})", line)
    if match:
        return re.sub(r"[A-Za-z0-9+/_=-]{37,}", "[id]", match[0])


def run(phase, labels, flags=()):
    work = ROOT / phase
    work.mkdir()
    result = {"labels": {}, "phase": phase}
    report["cases"][phase] = result
    bep = work / "bep"
    with (work / "bazel.log").open("w") as stream:
        result["exit"] = subprocess.run([
            "docker", "exec", "tcp-compiler", str(ROOT / "bazel"), "--bazelrc=" + str(ROOT / "client.rc"), "test",
            "--config=x86_64", "--jobs=64", "--keep_going", "--build_tag_filters=", "--test_tag_filters=",
            "--strategy=TestRunner=remote", "--noremote_local_fallback", "--nocache_test_results",
            "--modify_execution_info=^TestRunner$=+no-remote-cache", "--remote_download_outputs=toplevel",
            "--remote_download_regex=.*[/]test[.](log|xml)$", "--test_output=errors", "--test_timeout=450",
            "--test_sharding_strategy=disabled", "--build_event_json_file=" + str(bep), *flags, *labels],
            stdout=stream, stderr=subprocess.STDOUT).returncode
    save()
    attempts = {}
    if bep.is_file():
        for line in bep.open():
            event = json.loads(line)
            if "started" in event:
                result["invocation"] = event["started"]["uuid"]
            if "testResult" in event:
                label = event["id"]["testResult"]["label"]
                attempts.setdefault(label, []).append(event["testResult"])
    for label in labels:
        rows = attempts.get(label, [])
        record = {"attempts": len(rows)}
        result["labels"][label] = record
        if len(rows) != 1:
            continue
        attempt = rows[0]
        ex = attempt.get("executionInfo", {})
        record.update(status=attempt.get("status"), strategy=ex.get("strategy"),
                      cached=bool(attempt.get("cachedLocally") or ex.get("cachedRemotely")))
        files = attempt.get("testActionOutput", [])
        for name in ("test.log", "test.xml") if label == SYSCALL else ("test.log",):
            file = Path("bazel-testlogs") / label[2:].replace(":", "/") / name
            named = [f for f in files if f.get("name") == name]
            if not file.is_file() or len(named) != 1:
                continue
            data = file.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            meta = named[0]
            uri = meta.get("uri", "")
            remote = re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/(\d+)$", uri)
            if meta.get("digest"):
                bound = meta["digest"] == digest and int(meta.get("length", len(data))) == len(data)
            elif remote:
                bound = remote[1] == digest and int(remote[2]) == len(data)
            else:
                bound = uri.startswith("file:///") and Path(uri[7:]).exists() and file.samefile(uri[7:])
            record[name + "_bound"] = bool(bound)
            if not bound:
                continue
            if name == "test.xml":
                record["assertions"] = {tc.get("classname", "") + "." + tc.get("name", ""):
                    not any(tc.find(k) is not None for k in ("skipped", "error", "failure")) and
                    tc.get("status") != "notrun" and tc.get("result") != "suppressed"
                    for tc in ET.fromstring(data).iter("testcase")}
            else:
                text = data.decode(errors="replace")
                record["log_sha256"] = digest
                if label.endswith("_nogo"):
                    parts = re.split(r"(?m)^(amd64|arm64)\.\.\.", text)
                    record["sections"] = {arch: {"passed": body.strip() == "PASS",
                        "diagnostics": [d for l in body.splitlines() if (d := diagnostic(l))][:12]}
                        for arch, body in zip(parts[1::2], parts[2::2])}
                elif label != SYSCALL:
                    hits = re.findall(r"(?m)^\s*--- (PASS|FAIL|SKIP): (\S+)", text)
                    record["outcomes"] = dict(Counter(outcome for outcome, _ in hits))
                    record["rejected_control"] = ("PASS", "TestRejectedShutdownReadiness") in hits
    if result["exit"]:
        with (work / "bazel.log").open() as stream:
            result["diagnostics"] = [d for line in stream if (d := diagnostic(line))][:18]
    save()
    print(phase, result["exit"], len(attempts), result.get("invocation"), flush=True)


assert git("rev-parse", "HEAD") == PRODUCT
assert git("diff", "--name-only", "HEAD") == ""
save()
run("owners", OWNERS, ["--test_arg=-test.v"])
run("analysis", NOGO)
run("syscalls", [SYSCALL], ["--test_env=GTEST_FILTER=AllInetTests/TcpSocketTest.HalfCloseReadiness*:AllInetTests/TcpSocketTest.PollAfterShutdown/*"])
test = Path("pkg/tcpip/transport/tcp/endpoint_test.go")
build = test.with_name("BUILD")
old_test, old_build = test.read_text(), build.read_text()
try:
    assert old_test.count('"testing"') == 1
    test.write_text(old_test.replace('"testing"', '"context"\n\t"testing"\n\t"gvisor.dev/gvisor/pkg/tcpip"\n\t"gvisor.dev/gvisor/pkg/waiter"') + (ROOT / "rejected.go").read_text())
    prefix, rule = old_build.split('name = "tcp_test",', 1)
    assert '"//pkg/sync",' in rule
    build.write_text(prefix + 'name = "tcp_test",' + rule.replace('"//pkg/sync",', '"//pkg/sync",\n        "//pkg/waiter",', 1))
    run("rejected", [TCP + "tcp_test"], ["--test_arg=-test.v", "--test_arg=-test.run=^TestRejectedShutdownReadiness$"])
finally:
    test.write_text(old_test)
    build.write_text(old_build)
state = test.with_name("endpoint_state.go")
original = state.read_text()
mark = " // +checklocksforce: restore has exclusive access to e."
assert original.count(mark) == 1
force_line = original[:original.index(mark)].count("\n") + 1
report["restore_store_line"] = force_line
try:
    state.write_text(original.replace(mark, ""))
    run("restore-without-force", [TCP + "tcp_nogo"])
finally:
    state.write_text(original)
assert git("diff", "--name-only", "HEAD") == ""
good = True
for phase in ("owners", "analysis", "syscalls", "rejected"):
    r = report["cases"][phase]
    good &= r["exit"] == 0
    for label, row in r["labels"].items():
        ok = (row.get("attempts") == 1 and row.get("status") == "PASSED" and row.get("strategy") == "remote"
              and not row.get("cached") and row.get("test.log_bound"))
        if phase == "analysis":
            sections = row.get("sections", {})
            ok = ok and set(sections) == {"amd64", "arm64"} and all(v["passed"] for v in sections.values())
        elif phase == "syscalls":
            assertions = row.get("assertions", {})
            ok = ok and set(assertions) == GTEST and all(assertions.values())
        elif phase == "rejected":
            ok = ok and row.get("rejected_control")
        else:
            ok = ok and row.get("outcomes", {}).get("PASS", 0) > 0 and not row.get("outcomes", {}).get("FAIL")
        good &= bool(ok)
negative = report["cases"]["restore-without-force"]
row = negative["labels"][TCP + "tcp_nogo"]
sections = row.get("sections", {})
bound = (row.get("attempts") == 1 and row.get("strategy") == "remote" and not row.get("cached")
         and row.get("test.log_bound") and set(sections) == {"amd64", "arm64"})
if bound and negative["exit"] == 0 and row.get("status") == "PASSED" and all(part["passed"] for part in sections.values()):
    outcome = "unforced store accepted on both architectures"
elif (bound and negative["exit"] != 0 and row.get("status") == "FAILED" and
      all(not part["passed"] and any(re.search(rf"endpoint_state\.go:{force_line}(?::\d+)?[: ]", diagnostic)
          and re.search(r"lock|atomic|guard", diagnostic, re.I) for diagnostic in part["diagnostics"]) for part in sections.values())):
    outcome = "unforced restore store diagnosed on both architectures"
else:
    outcome = "inconclusive"
report["restore_control_outcome"] = outcome
report["production_and_control_verified"] = bool(good and outcome != "inconclusive")
save()
raise SystemExit(not report["production_and_control_verified"])
