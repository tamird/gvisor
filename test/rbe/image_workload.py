"""Run the focused RACK red/green comparison before interpreting either arm."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

import qualification_report as report

PACKAGE = "//pkg/tcpip/transport/tcp/test/e2e:"
RACK = PACKAGE + "tcp_rack_test"
SACK = PACKAGE + "tcp_sack_test"
SDK = "@io_bazel_rules_go//go:go"
PYTHON = "@rules_python//python/bin:python"
REGRESSION = {"TestRACKPipeAfterTimeout", "TestRACKPipeAfterTimeout/gso_0", "TestRACKPipeAfterTimeout/gso_2"}
RACK_CASES = REGRESSION | {
    "TestRACKUpdateSackedOut", *["TestRACKUpdateSackedOut/" + name for name in (
        "two_sacks", "without_rack", "recovery", "timeout", "disjoint_sacks",
        "overlapping_sacks_across_acks", "overlapping_sacks_one_ack",
        "overlapping_sacks_with_cumulative_ack", "gso_partial_ack")],
    "TestRACKTLPFallbackRTO", "TestRACKTLPFallbackRTO/reno", "TestRACKTLPFallbackRTO/cubic",
    "TestRACKWithWindowFull",
}
SACK_CASES = {"TestSACKRecovery", "TestRecoveryEntry", "TestRecoveryEntry/reno",
              "TestRecoveryEntry/cubic", "TestDetectSpuriousRecoveryWithRTO",
              "TestSACKDetectSpuriousRecoveryWithDupACK", "TestNoSpuriousRecoveryWithDSACK"}


def check_phase(evidence: Path, phase: dict, paths: set[Path]) -> dict:
    """Join actual uncached owners to their BEP reports and exact XML leaves."""
    directory = evidence / ("invocation-" + phase["name"])
    result = phase["result"]
    owners = phase["owners"]
    red = phase["kind"] == "red"
    wanted = "FAIL" if red else "PASS"
    if (result["source"] != phase["source"] or result["status"] != wanted
            or (result["native_exit"] == 0) == red
            or (directory / "head.txt").read_text().strip() != phase["source"]
            or json.loads((directory / "context.json").read_text())["expected_source"] != phase["source"]
            or int((directory / "native-exit").read_text()) != result["native_exit"]
            or int((directory / "capture-exit").read_text()) != 0
            or (directory / "before.txt").read_text() or (directory / "after.txt").read_text()
            or (directory / "expected-targets.txt").read_text().splitlines() != owners):
        raise ValueError(f"{phase['name']}: native/source/owner result differs")
    attempts = result["executed"]
    if len(attempts) != len(owners) or {a["owner"]["label"] for a in attempts} != set(owners):
        raise ValueError(f"{phase['name']}: actual owner census differs")
    manifest_path = report.manifest_path(directory / "events.jsonl")
    manifest = json.loads(manifest_path.read_text())
    _, _, errors = report.retained_reports(manifest_path)
    if errors or report.file_record(directory / "events.jsonl") != manifest["events"]:
        raise ValueError(f"{phase['name']}: invalid retained event/report hashes: {errors}")
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    native = {json.dumps(e["id"]["testResult"], sort_keys=True): e["testResult"]
              for e in events if "testResult" in e.get("id", {})}
    if len(native) != len(owners):
        raise ValueError(f"{phase['name']}: native attempt census differs")
    checked = []
    for attempt in attempts:
        owner = attempt["owner"]["label"]
        identity = {"label": owner, "configuration": {"id": attempt["owner"]["configuration"]},
                    "run": 1, "attempt": 1, "shard": 1}
        row = native.get(json.dumps(identity, sort_keys=True))
        status = "FAILED" if red else "PASSED"
        if (row is None or row["status"] != status or attempt["status"] != status
                or (attempt["run"], attempt["attempt"], attempt["shard"]) != (1, 1, 1)
                or attempt["cached_locally"] or row.get("cachedLocally", False)
                or row.get("executionInfo", {}).get("cachedRemotely", False)
                or row.get("executionInfo", {}).get("strategy") != "local"):
            raise ValueError(f"{phase['name']}: failed, cached or unexpected native attempt")
        reports = [r for r in manifest["reports"] if r["identity"] == identity]
        if sorted(r["name"] for r in reports) != ["test.log", "test.xml"]:
            raise ValueError(f"{phase['name']}: missing exact log/XML pair")
        bodies = {}
        for item in reports:
            path = (directory / item["path"]).resolve()
            path.relative_to(directory.resolve())
            outputs = [out for out in row["testActionOutput"] if out["name"] == item["name"]]
            if (path in paths or len(outputs) != 1
                    or not outputs[0]["uri"].endswith("/" + item["path"].removeprefix("reports/"))
                    or report.file_record(path) != {k: item[k] for k in ("bytes", "sha256")}):
                raise ValueError(f"{phase['name']}: reused path or native output/hash mismatch")
            paths.add(path)
            bodies[item["name"]] = path.read_bytes()
        cases = list(ET.fromstring(bodies["test.xml"]).iter("testcase"))
        names = [case.attrib["name"] for case in cases]
        log = bodies["test.log"].decode()
        if phase["kind"] == "nogo":
            if len(cases) != 1 or any(list(case) for case in cases):
                raise ValueError(f"{phase['name']}: analyzer XML differs")
            if not all(cpu + "...PASS" in log for cpu in ("amd64", "arm64")):
                raise ValueError(f"{phase['name']}: missing both analyzer CPUs")
        else:
            expected = REGRESSION if red else RACK_CASES if owner == RACK else SACK_CASES
            if len(names) != len(expected) or set(names) != expected:
                raise ValueError(f"{phase['name']}: XML cases {names}, expected {sorted(expected)}")
            for case in cases:
                failures = [x for x in case if x.tag in ("failure", "error")]
                if any(x.tag == "skipped" for x in case) or bool(failures) != red:
                    raise ValueError(f"{phase['name']}: unexpected per-case failure/skip")
            if red:
                diagnostics = [line.strip() for line in log.splitlines() if "post-RTO RACK pipe:" in line]
                if (len(diagnostics) != 2 or any("active=true cwnd=5 outstanding=10, want true/5/5" not in line
                                                 for line in diagnostics)):
                    raise ValueError(f"{phase['name']}: baseline did not reproduce the exact pipe defect: {diagnostics}")
        checked.append({"identity": identity, "cases": names, "status": status,
                        "reports": reports})
    if len(manifest["reports"]) != 2 * len(owners):
        raise ValueError(f"{phase['name']}: extra report identity")
    return {"owners": checked}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    arms = config["arms"]
    if (config.get("draft", True) or [a["name"] for a in arms] != ["before", "after"]
            or len({a["source"] for a in arms}) != 2
            or report.source_set(config["expected_sources"]) != {a["source"] for a in arms}
            or config["source"] != arms[-1]["source"]):
        raise ValueError("Unbound two-arm RACK contract")
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"],
               "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    tests = ["--strategy=TestRunner=local", "--nocache_test_results", "--runs_per_test=1",
             "--flaky_test_attempts=1", "--test_output=all", "--test_timeout=300",
             "--test_sharding_strategy=disabled", "--test_tag_filters=", "--keep_going",
             "--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=1"]
    record = {"status": "INCOMPLETE", "source": config["source"],
              "expected_sources": config["expected_sources"], "arms": [], "restored": False,
              "scope": "Expected native baseline failure is retained; candidate behavior is assessed separately."}
    command_errors = []
    try:
        sdk = invoke("declared-go-sdk", ["run", *options, SDK, "--", "env", "-json", "GOROOT", "GOVERSION"],
                     "command", targets=[])
        if sdk["status"] != "PASS":
            raise ValueError("Declared SDK query failed")
        env_path = evidence / "invocation-declared-go-sdk" / "stdout.log"
        match json.loads(env_path.read_text()):
            case {"GOROOT": str(root), "GOVERSION": "go1.27.3"}:
                sdk_root = Path(root)
            case other:
                raise ValueError(f"Unexpected SDK: {other}")
        if not sdk_root.is_absolute() or not sdk_root.is_dir():
            raise ValueError("SDK root is not an existing absolute directory")
        record["sdk_environment"] = report.file_record(env_path)
        record["tools"] = {name: report.file_record(sdk_root / "bin" / name) for name in ("go", "gofmt")}
        for arm in arms:
            source = arm["source"]
            subprocess.run(["git", "checkout", "--detach", source], check=True, timeout=30)
            if (subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != source
                    or subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"])):
                raise ValueError("Source switch did not reach a clean arm")
            for name, expected in arm["matched_inputs"].items():
                path = Path(name)
                body = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
                if hashlib.sha256(body).hexdigest() != expected:
                    raise ValueError(f"{arm['name']}: source input differs: {name}")
            item = {"name": arm["name"], "source": source, "phases": []}
            record["arms"].append(item)
            original_path = os.environ["PATH"]
            try:
                os.environ["PATH"] = str(sdk_root / "bin") + os.pathsep + original_path
                selected_go = shutil.which("go")
                if selected_go is None or not (sdk_root / "bin" / "go").samefile(selected_go):
                    raise ValueError("Canonical lint would not use declared SDK")
                name = arm["name"] + "-gofmt"
                result = invoke(name, ["run", *options, PYTHON, "--", "-c",
                    'import os, subprocess, sys; os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"]); sys.exit(subprocess.run(["bash", "tools/lint.sh", "gofmt"]).returncode)'],
                    "command", targets=[], source=source)
                if result["status"] != "PASS":
                    command_errors.append(name + ": formatter failed")
            finally:
                os.environ["PATH"] = original_path
            selected = [("regression", "red", [RACK], "^TestRACKPipeAfterTimeout$", False)] if arm["name"] == "before" else [
                ("rack-normal", "green", [RACK], "^(TestRACKPipeAfterTimeout|TestRACKUpdateSackedOut|TestRACKTLPFallbackRTO|TestRACKWithWindowFull)$", False),
                ("rack-race", "green", [RACK], "^(TestRACKPipeAfterTimeout|TestRACKUpdateSackedOut|TestRACKTLPFallbackRTO|TestRACKWithWindowFull)$", True),
                ("sack-normal", "green", [SACK], "^(TestSACKRecovery|TestRecoveryEntry|TestDetectSpuriousRecoveryWithRTO|TestSACKDetectSpuriousRecoveryWithDupACK|TestNoSpuriousRecoveryWithDSACK)$", False),
                ("sack-race", "green", [SACK], "^(TestSACKRecovery|TestRecoveryEntry|TestDetectSpuriousRecoveryWithRTO|TestSACKDetectSpuriousRecoveryWithDupACK|TestNoSpuriousRecoveryWithDSACK)$", True),
                ("nogo", "nogo", [RACK + "_nogo", SACK + "_nogo"], None, False),
            ]
            for suffix, kind, owners, pattern, race in selected:
                name = arm["name"] + "-" + suffix
                extra = (["--@io_bazel_rules_go//go/config:race=true"] if race else [])
                if pattern is not None:
                    extra.append("--test_arg=-test.run=" + pattern)
                result = invoke(name, ["test", *options, *tests, *extra, *owners], "test", targets=owners, source=source)
                item["phases"].append({"name": name, "kind": kind, "source": source,
                                       "owners": owners, "result": result})
        # Preserve every native result before interpreting expected red or green.
        observed = {path.parent.name.removeprefix("invocation-"): json.loads(path.read_text())["source"]
                    for path in evidence.glob("invocation-*/verdict.json")}
        if observed != config["expected_phase_sources"]:
            raise ValueError("Actual phase/source mapping differs from the plan")
        if command_errors:
            raise ValueError("; ".join(command_errors))
        paths = set()
        for arm in record["arms"]:
            for phase in arm["phases"]:
                phase["checked"] = check_phase(evidence, phase, paths)
        record.update(status="PASS", baseline_native_status="FAIL", candidate_status="PASS")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        record.update(status="FAIL", error=str(error))
        raise
    finally:
        try:
            subprocess.run(["git", "checkout", "--detach", config["source"]], check=True, timeout=30)
            if (subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != config["source"]
                    or subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"])):
                raise ValueError("Failed to restore initial qualification source")
            record["restored"] = True
        finally:
            report.write_json(evidence / "tcp-rack-pipe.json", record)
