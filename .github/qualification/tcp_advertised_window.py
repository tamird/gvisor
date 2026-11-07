"""Check the advertised window under the endpoint lock and the receive queue lock."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree


WORK = Path(os.environ["RUNNER_TEMP"]) / "tcp-advertised-window"
BASE = "9ee2906f518e1d0f9fd109e9e1feb2812096418b"
PRODUCT = "3e626e8ca498199e41510a594fcf9c9192c561b4"
PACKAGE = Path("pkg/tcpip/transport/tcp")
TEST = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
NOGO = "//pkg/tcpip/transport/tcp:tcp_nogo"
CASE = "TestZeroScaledWindowReceive"
SOURCE = PACKAGE / "rcv.go"
ACCESS = "r.ep.RcvAutoParams.RTTMeasureSeqNumber = r.RcvNxt.Add(r.rcvWnd)"
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
    row.update(source=git("rev-parse", "HEAD"), receiverBlob=git("hash-object", str(SOURCE)), owner=owner)
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
    negative = phase == "queue_lock_only"
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
    lines = SOURCE.read_text().splitlines()
    if negative:
        row["accessLines"] = [i for i, line in enumerate(lines, 1) if line.strip() == ACCESS]
        assert len(row["accessLines"]) == 2
    row["findings"] = {}
    for arch in ARCHES:
        raw = (Path("bazel-bin") / PACKAGE / ("tcp_nogo." + arch + ".findings")).read_bytes()
        findings = json.loads(raw) or []
        matches = [f for f in findings if f["Category"] == "checklocks" and f["GOARCH"] == arch
                   and f["GOOS"] == "linux" and Path(f["Position"]["Filename"]).name == "rcv.go"
                   and f["Position"]["Line"] in row.get("accessLines", [])
                   and "invalid field access, ep.mu " in f["Message"]
                   and "must be locked when accessing rcvWnd" in f["Message"]
                   and "rcvQueueMu" in f["Message"].partition("(locks: ")[2]]
        positions = sorted({f["Position"]["Line"] for f in matches})
        row["findings"][arch] = {"count": len(findings), "matching": len(matches), "atLines": positions,
                                 "guardMessages": [f["Message"][:400] for f in findings if
                                     f["Category"] == "checklocks" and
                                     Path(f["Position"]["Filename"]).name in ("rcv.go", "connect.go")][:8],
                                 "sha256": hashlib.sha256(raw).hexdigest()}
        valid &= (positions == row["accessLines"] and len(matches) == len(findings)) if negative else not findings
    return valid


def main():
    report = {"product": PRODUCT, "parent": BASE, "arms": {}, "verified": False}
    record(report)
    fixed = git("rev-parse", PRODUCT + ":" + str(SOURCE))
    assert fixed == "6b4842da09804e009e96418ece35b2086fb22448"
    for phase in ("runtime", "field_and_method_guards", "queue_lock_only", "stock"):
        row = report["arms"][phase] = {"verified": False}
        try:
            ref = BASE if phase == "stock" else PRODUCT
            subprocess.run(["git", "switch", "--detach", ref], check=True)
            assert git("rev-parse", "HEAD") == ref and not git("diff", "--name-only", "HEAD")
            if phase == "queue_lock_only":
                text = SOURCE.read_text()
                guard = "// +checklocks:r.ep.mu\n"
                method = "func (r *receiver) updateRTT("
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
