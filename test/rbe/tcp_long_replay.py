import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import qualification_report as reporting


def main():
    out, arm, source, label, raw, proof = sys.argv[1:]
    out, raw, proof = Path(out), Path(raw), Path(proof)
    out.joinpath("targets.txt").write_text(label + "\n")
    captured = reporting.capture(raw, out / "events.jsonl", Path("bazel-out"), out / "reports")
    (out / "capture_exit").write_text("1" if captured["errors"] else "0")
    context = {k: k for k in ("commit_file", "source_before", "source_after", "final_head", "executed_arguments", "native_exit", "capture_exit")}
    context.update(expected_source=source, command_kind="test", execution_finished_at=(out / "finished").read_text().strip())
    reporting.write_json(out / "context.json", context)
    events = out / "events.jsonl"
    verdict = reporting.verdict(events, reporting.manifest_path(events), out / "context.json", out / "targets.txt", ())
    proof.mkdir(exist_ok=True, parents=True)
    errors = []
    logs = [(out / row["path"]).read_text(errors="replace") for row in captured["reports"] if row["name"] == "test.log"]
    log = "\n".join(logs)
    suite = "TestKeepaliveUserTimeout"
    wanted_failure = suite + "/UserTimeout"
    expected = {"ProbeCount": "PASS", "UserTimeout": "FAIL" if arm == "stock" else "PASS"}
    observations = {}
    unexpected_failures = set()

    def observe(name, state):
        if name.startswith(suite + "/"):
            subcase = name.removeprefix(suite + "/")
            if subcase in observations and observations[subcase] != state:
                errors.append("inconsistent results for " + name)
            observations[subcase] = state
        if state == "FAIL" and name not in (suite, wanted_failure, label, label.rsplit(":", 1)[-1]):
            unexpected_failures.add(name)

    for state, name in re.findall(r"--- (PASS|FAIL|SKIP): (\S+) \(", log):
        observe(name, state)
    for row in captured["reports"]:
        if row["name"] == "test.xml":
            for case in ET.parse(out / row["path"]).iter("testcase"):
                full = case.get("name", "")
                state = "FAIL" if case.find("failure") is not None or case.find("error") is not None else "SKIP" if case.find("skipped") is not None else "PASS"
                observe(full, state)
    results = [e["testResult"] for line in events.read_text().splitlines() if "testResult" in (e := json.loads(line))]
    invocation = next((e["started"]["uuid"] for line in events.read_text().splitlines() if "started" in (e := json.loads(line))), None)
    if observations != expected:
        errors.append("actual keepalive cases differ from expected")
    if unexpected_failures:
        errors.append("unrelated failed cases: " + ", ".join(sorted(unexpected_failures)))
    observed_timeout = "second keepalive probe: SO_ERROR = connection timed out;" in log
    if arm == "stock" and not observed_timeout:
        errors.append("missing second probe did not report ETIMEDOUT")
    if len(results) != 1 or any(r.get("cachedLocally") or r.get("executionInfo", {}).get("cachedRemotely") or r.get("executionInfo", {}).get("strategy") != "remote" for r in results):
        errors.append("expected one uncached remote result")
    runner_exit = int((out / "runner_exit").read_text())
    if runner_exit != (1 if arm == "stock" else 0):
        errors.append("unexpected qualification wrapper exit")
    wanted_status = "FAILED" if arm == "stock" else "PASSED"
    if len(results) == 1 and results[0].get("status") != wanted_status:
        errors.append("unexpected Bazel test status")
    if any(f.get("status", wanted_status) != wanted_status for f in verdict["failures"]):
        errors.append("unrelated test summary failure")
    verdict["errors"].extend(errors)
    wanted_exit = 3 if arm == "stock" else 0
    matched = bool(verdict["native_exit"] == wanted_exit and verdict["status"] == ("FAIL" if arm == "stock" else "PASS") and not verdict["errors"] and not verdict["missing"] and not verdict["skips"])
    # Keep the negative control's original status even when its failure was expected.
    verdict.update(arm=arm, invocation=invocation, platform="amd64", cases=observations,
                   runner_exit=runner_exit, second_probe_errno="ETIMEDOUT" if observed_timeout else None,
                   matches_expectation=matched)
    reporting.write_json(proof / (arm + ".json"), verdict)
    print(json.dumps({k:verdict[k] for k in ("arm","invocation","status","errors","missing","cases","matches_expectation","execution_to_verdict_seconds")}), flush=True)
    return int(not matched)

if __name__ == "__main__":
    sys.exit(main())
