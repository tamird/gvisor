"""Run the generic-snapshot kernel regression using the existing reporter."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import traceback

import qualification_report as report


BEFORE = "78f826dd61c078b907ec9328385ea56d01741bfc"
AFTER = "7c8ace19ecf563a0417c2fa39575e04eb7851e5e"
TARGET = "//enterprise/server/remote_execution/containers/firecracker:guest_kernel_test"
PREFIX = "firecracker/TestGuestKernelBootAndSnapshot/generic_snapshot_lookup/"
CASES = frozenset(PREFIX + name for name in
                  ("default", "explicit_upgrade", "rollout", "explicit_beats_rollout"))
EXPECTED_FAILURES = frozenset(PREFIX + name for name in ("explicit_upgrade", "explicit_beats_rollout"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def expected_before(xml: dict[str, object]) -> bool:
    allowed = {"/".join(name.split("/")[:i]) for name in EXPECTED_FAILURES
               for i in range(2, len(name.split("/")) + 1)}
    failures = set(xml["failures"])
    return (EXPECTED_FAILURES <= failures <= allowed and not xml["skipped"]
            and set(xml["required_passed"]) == CASES - EXPECTED_FAILURES)


def controls() -> None:
    original = {"failures": sorted(EXPECTED_FAILURES), "skipped": 0,
                "required_passed": sorted(CASES - EXPECTED_FAILURES)}
    assert expected_before(original), "the original two expected failures must be accepted"
    for changed in (
        {**original, "failures": []},
        {**original, "failures": [*EXPECTED_FAILURES, "firecracker/TestOther"]},
        {**original, "skipped": 1},
        {**original, "required_passed": []},
    ):
        assert not expected_before(changed), "incomplete or unrelated failures must be rejected"


def qualify(variant: str, source: str, tmp: Path, out: Path) -> dict[str, object]:
    git("checkout", "--quiet", "--detach", source)
    private = tmp / ("generic-kernel-private-" + variant)
    private.mkdir()
    public = out / variant
    public.mkdir()
    row = {"status": "INCOMPLETE", "source": source, "target": TARGET, "phases": {}}
    write(public / "receipt.json", row)
    for name, body in (("commit", git("cat-file", "commit", source)), ("before", git("diff", "HEAD", "--"))):
        (private / name).write_bytes(body)
    (private / "expected").write_text(TARGET + "\n")
    write(private / "cases.json", {TARGET: sorted(CASES)})
    bazel = ["bazelisk", "--bazelrc=" + str(tmp / "generic-kernel-private.bazelrc")]
    common = ["--config=ci-anon", "--config=target-linux-x86", "--config=tests-ci", "--noannounce_rc",
              "--lockfile_mode=error", "--jobs=400", "--keep_going", "--nocache_test_results",
              "--flaky_test_attempts=1", "--spawn_strategy=remote", "--strategy=TestRunner=remote",
              "--noremote_local_fallback", "--remote_download_outputs=toplevel", "--test_timeout=180",
              "--nobuild_event_json_file_path_conversion", "--color=no", "--curses=no"]
    for phase in ("analysis", "test"):
        raw = private / (phase + ".bep")
        options = ["--nobuild"] if phase == "analysis" else ["--execution_log_compact_file=" + str(private / "actions.compact")]
        args = ["test", *common, *options, "--build_event_json_file=" + str(raw), TARGET]
        start = now()
        with (private / (phase + ".log")).open("w") as log:
            code = subprocess.run(["timeout", "--signal=INT", "--kill-after=30s", "35m", *bazel, *args],
                                  stdout=log, stderr=subprocess.STDOUT).returncode
        finished = now()
        row["phases"][phase] = {"native_exit": code, "started_at": start, "execution_finished_at": finished}
        log_text = (private / (phase + ".log")).read_text(errors="replace")
        row["phases"][phase]["invocations"] = sorted(set(re.findall(
            r"https://app\.buildbuddy\.io/invocation/([0-9a-f-]{36})", log_text)))
        row["phases"][phase]["compiler_diagnostics"] = [line[:600] for line in log_text.splitlines()
            if re.match(r"[^\s:]+\.go:\d+:\d+: ", line)
            and not re.search(r"(?i)credential|token|api.key|authorization|header|https?://", line)][:15]
        write(public / "receipt.json", row)
        if phase == "analysis":
            preflight = report.preflight(raw, code, "test", private / "cases.json")
            write(public / "analysis.json", preflight)
            if preflight["status"] != "PASS":
                return row
            continue
        (private / "arguments").write_text("\n".join(args) + "\n")
        (private / "native_exit").write_text(str(code))
        (private / "after").write_bytes(git("diff", "HEAD", "--"))
        (private / "head").write_bytes(git("rev-parse", "HEAD"))
        execroot = Path(subprocess.check_output([*bazel, "info", "execution_root"], stderr=subprocess.DEVNULL, text=True).strip())
        sanitized = private / "sanitized.bep"
        capture = report.capture(raw, sanitized, execroot, private / "reports", False, (private / "analysis.bep",), "test")
        (private / "capture_exit").write_text(str(int(bool(capture["errors"]))))
        write(private / "context.json", {
            "expected_source": source, "commit_file": "commit", "source_before": "before", "source_after": "after",
            "final_head": "head", "executed_arguments": "arguments", "native_exit": "native_exit",
            "capture_exit": "capture_exit", "execution_finished_at": finished, "command_kind": "test",
        })
        verdict = report.verdict(sanitized, report.manifest_path(sanitized), private / "context.json",
                                 private / "expected", (private / "analysis.bep",), case_manifest=private / "cases.json")
        write(public / "tests.json", verdict)
        row["runtime_status"] = verdict["status"]
        write(public / "receipt.json", row)
        executions = []
        for line in raw.read_text().splitlines():
            event = json.loads(line)
            if "testResult" in event:
                result = event["testResult"]
                info = result.get("executionInfo", {})
                executions.append({"target": event["id"]["testResult"]["label"], "status": result["status"],
                                   "strategy": info.get("strategy"),
                                   "cached": bool(result.get("cachedLocally") or info.get("cachedRemotely"))})
        row["execution"] = executions
        match executions:
            case [{"target": str() as target, "strategy": "remote", "cached": False}] if target == TARGET:
                remote = True
            case _:
                remote = False
        xmls = list((private / "reports").rglob("test.xml"))
        if len(xmls) != 1:
            row["reason"] = "Expected one original XML report"
        else:
            (xml,) = xmls
            shutil.copyfile(xml, public / "test.xml")
            facts = report.xml_cases(xml, CASES)
            row["xml"] = facts
            complete = remote and not verdict["errors"] and not verdict["missing"]
            if variant == "before":
                if complete and code == 3 and verdict["status"] == "FAIL" and expected_before(facts):
                    row["status"] = "EXPECTED_FAILURE"
            elif complete and verdict["status"] == "PASS" and not facts["skipped"]:
                row["status"] = "PASS"
        row["verified_at"] = now()
        write(public / "receipt.json", row)
    return row


def main() -> None:
    tmp = Path(os.environ["RUNNER_TEMP"])
    out = tmp / "generic-kernel-evidence"
    out.mkdir(exist_ok=True)
    controls()
    result = {"status": "INCOMPLETE", "before_source": BEFORE, "after_source": AFTER, "predicate_controls": 5,
              "scope": "Ordinary constructor and cached generic snapshot lookup; no VM boot or on-demand VM rebuild"}
    write(out / "verdict.json", result)
    for variant, source in (("before", BEFORE), ("after", AFTER)):
        try:
            result[variant] = qualify(variant, source, tmp, out)
        except Exception as err:
            frame = traceback.extract_tb(err.__traceback__)[-1]
            result[variant] = {"status": "INCOMPLETE", "source": source, "error_type": type(err).__name__,
                               "error_location": {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}}
        write(out / "verdict.json", result)
        if result[variant]["status"] == "INCOMPLETE":
            break
    if (result.get("before", {}).get("status") == "EXPECTED_FAILURE"
            and result.get("after", {}).get("status") == "PASS"):
        result["status"] = "PASS"
    result["verified_at"] = now()
    write(out / "verdict.json", result)
    print(json.dumps(result))
    raise SystemExit(result["status"] != "PASS")


if __name__ == "__main__":
    main()
