"""Verify the endpoint mutex guarding the TCP probe count and timeout baseline."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree


WORK = Path(os.environ["RUNNER_TEMP"]) / "tcp-persist-state"
BASE = "7d1b049f5d541992b47cc38cf564025908a9ed7b"
PRODUCT = "455572aa75bf2f6f9c5c4a41661dae78253618e2"
PACKAGE = Path("pkg/tcpip/transport/tcp")
TEST = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
NOGO = "//pkg/tcpip/transport/tcp:tcp_nogo"
CASE = "TestZeroWindowSend"
SOURCE = PACKAGE / "snd.go"
RESET = "func (s *sender) disableZeroWindowProbing() {"
FIELDS = {"zeroWindowProbing", "unackZeroWindowProbes", "firstRetransmittedSegXmitTime"}
ARCHES = ("amd64", "arm64")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def record(report):
    (WORK / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def original_output(attempt, name, owner):
    item, = [f for f in attempt["testActionOutput"] if f["name"] == name]
    package, target = owner.removeprefix("//").split(":")
    path = Path("bazel-testlogs") / package / target / name
    uri = urlsplit(item["uri"])
    assert uri.scheme == "file" and not uri.netloc and Path(unquote(uri.path)).resolve() == path.resolve()
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    assert not item.get("digest") or item["digest"] == sha
    return data, sha


def measure(phase, row):
    owner = TEST if phase == "runtime" else NOGO
    events = WORK / (phase + ".bep")
    flags = ["--test_filter=^" + CASE + "$", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v"] if owner == TEST else []
    args = [WORK / "bazel", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g", "test",
            "--config=x86_64", "-c", "opt", "--jobs=4", "--lockfile_mode=off",
            "--remote_cache=", "--remote_executor=", "--bes_backend=",
            "--strategy=TestRunner=local", "--test_sharding_strategy=disabled",
            "--nocache_test_results", "--runs_per_test=1", "--flaky_test_attempts=1",
            "--build_tag_filters=", "--test_tag_filters=", "--test_timeout=180",
            "--build_event_json_file=" + str(events), *flags, owner]
    row.update(source=git("rev-parse", "HEAD"), senderBlob=git("hash-object", str(SOURCE)), owner=owner)
    with (WORK / (phase + ".out")).open("wb") as log:
        row["exit"] = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT).returncode
    if not events.is_file():
        return False
    ends, attempts = [], []
    for line in events.open():
        if not line.endswith("\n"):
            continue
        event = json.loads(line)
        if "started" in event:
            row["invocation"] = event["started"]["uuid"]
        if "finished" in event:
            ends.append(event["finished"]["exitCode"].get("code", 0))
        if "testResult" in event:
            assert event["id"]["testResult"]["label"] == owner
            attempts.append(event["testResult"])
    row["finished"] = ends
    row["attempts"] = len(attempts)
    if len(attempts) != 1:
        output = (WORK / (phase + ".out")).read_text(errors="replace")
        row["diagnostics"] = [s for s in ("no such target", "no such package", "missing strict dependencies",
                              "no space left", "failed to build", "out of memory", "error downloading",
                              "error compiling", "not declared by package") if s in output.lower()]
        return False
    attempt, = attempts
    execution = attempt.get("executionInfo", {})
    row.update(status=attempt["status"], strategy=execution.get("strategy"),
               cached=attempt.get("cachedLocally", False) or execution.get("cachedRemotely", False))
    data, row["logSHA256"] = original_output(attempt, "test.log", owner)
    log = data.decode(errors="replace")
    negative = phase == "reset_without_lock"
    code = 3 if negative else 0
    valid = (row["exit"] == code and ends == [code] and row["strategy"] in ("local", "standalone")
             and not row["cached"] and row["status"] == ("FAILED" if negative else "PASSED"))
    if owner == TEST:
        xml, row["xmlSHA256"] = original_output(attempt, "test.xml", owner)
        cases = ElementTree.fromstring(xml).findall(".//testcase")
        row["namedXML"] = len(cases) == 1 and cases[0].get("name", "").rsplit(".", 1)[-1] == CASE
        row["xmlCases"] = len(cases)
        row["xmlClean"] = not any(c.get("status") in ("skipped", "notrun")
                                  or c.get("result") in ("skipped", "suppressed")
                                  or any(c.find(t) is not None for t in ("skipped", "failure", "error"))
                                  for c in cases)
        log = log.replace("\x16", "")
        row["goRuns"] = re.findall(r"(?m)^=== RUN\s+(Test\w+)\s*$", log)
        row["goResults"] = re.findall(r"(?m)^--- (PASS|FAIL|SKIP): (Test\w+)", log)
        return (valid and row["xmlCases"] == 1 and row["xmlClean"] and row["goRuns"] == [CASE]
                and row["goResults"] == [("PASS", CASE)])
    sections = re.findall(r"(?m)^(amd64|arm64)\.\.\.(PASS)?", log)
    row["sections"] = sections
    valid &= sorted(sections) == [(a, "" if negative else "PASS") for a in ARCHES]
    expected: dict[int, str] = {}
    if negative:
        lines = SOURCE.read_text().splitlines()
        start = lines.index(RESET)
        end = lines.index("}", start + 1)
        for pos in range(start + 1, end):
            if match := re.match(r"\s*s\.(\w+) = ", lines[pos]):
                expected[pos + 1] = match[1]
        assert set(expected.values()) == FIELDS and len(expected) == 3, expected
    row["findings"] = {}
    for arch in ARCHES:
        raw = (Path("bazel-bin") / PACKAGE / ("tcp_nogo." + arch + ".findings")).read_bytes()
        findings = json.loads(raw) or []
        matches = {}
        for finding in findings:
            pos = finding["Position"]
            line = pos["Line"]
            message = finding["Message"]
            if (finding["Category"] == "checklocks" and finding["GOOS"] == "linux"
                and finding["GOARCH"] == arch and Path(pos["Filename"]).name == "snd.go"
                and line in expected and "invalid field access, ep.mu " in message
                and ("must be locked when accessing " + expected[line]) in message):
                matches[line] = expected[line]
        row["findings"][arch] = {
            "count": len(findings), "accesses": matches, "sha256": hashlib.sha256(raw).hexdigest(),
            "messages": [f["Message"][:350] for f in findings[:8]],
        }
        valid = valid and matches == expected and len(findings) == len(expected)
    return valid


def main():
    report = {"product": PRODUCT, "parent": BASE, "arms": {}, "verified": False}
    record(report)
    fixed = git("rev-parse", PRODUCT + ":" + str(SOURCE))
    assert fixed == "9d004c50ef75a18c390877322d49713e02263888"
    for phase in ("runtime", "field_and_method_guards", "reset_without_lock", "stock"):
        row = report["arms"][phase] = {"verified": False}
        try:
            ref = BASE if phase == "stock" else PRODUCT
            subprocess.run(["git", "switch", "--detach", ref], check=True)
            assert git("rev-parse", "HEAD") == ref and not git("diff", "--name-only", "HEAD")
            if phase == "reset_without_lock":
                text = SOURCE.read_text()
                guard = "// +checklocks:s.ep.mu\n"
                method = "func (s *sender) disableZeroWindowProbing("
                assert text.count(guard + method) == 1
                SOURCE.write_text(text.replace(guard + method, method))
            row["verified"] = measure(phase, row)
        except (AssertionError, KeyError, OSError, ValueError, subprocess.SubprocessError, ElementTree.ParseError) as exc:
            row["error"] = type(exc).__name__
        finally:
            subprocess.run(["git", "restore", "--source=HEAD", "--", str(SOURCE)], check=True)
            record(report)
    report["verified"] = all(r["verified"] for r in report["arms"].values())
    record(report)
    return report["verified"]


if __name__ == "__main__":
    raise SystemExit(not main())
