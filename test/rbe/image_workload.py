"""Qualify one independent kernel locking or annotation split."""
import hashlib
import json
import os
import subprocess
from pathlib import Path
import xml.etree.ElementTree as ET

import qualification_report as report


UNITS = ["//pkg/sentry/kernel:kernel_test"]
ANALYZERS = ["//pkg/sentry/kernel:kernel_nogo", "//pkg/sentry/kernel:kernel_test_nogo"]


def check_reports(evidence: Path, name: str, owners: list[str], result: dict) -> list[dict]:
    counts = result["case_counts"]
    if (result["status"] != "PASS" or counts["failed"] or counts["skipped"]
            or not counts["passed"]):
        raise RuntimeError(f"{name}: require positive native PASS without skipped/failed cases")
    attempts = result["executed"]
    if len(attempts) != len(owners) or {a["owner"]["label"] for a in attempts} != set(owners):
        raise ValueError(f"{name}: executed owners differ")
    for attempt in attempts:
        if (attempt["status"] != "PASSED" or attempt["run"] != 1 or attempt["attempt"] != 1
                or attempt["shard"] != 1 or attempt["cached_locally"]
                or attempt["execution_info"].get("cachedRemotely", False)
                or attempt["execution_info"].get("strategy") != "local"):
            raise ValueError(f"{name}: expected one uncached coordinator execution per owner")
    directory = evidence / ("invocation-" + name)
    _, reports, errors = report.retained_reports(report.manifest_path(directory / "events.jsonl"))
    if errors:
        raise ValueError(f"{name}: invalid retained reports: {errors}")
    checks = []
    for owner in owners:
        selected = {(attempt, kind): path for (attempt, kind), path in reports.items()
                    if attempt.owner.label == owner}
        if sorted(kind for _, kind in selected) != ["test.log", "test.xml"]:
            raise ValueError(f"{name}: expected unique log/XML for {owner}")
        log = next(path for (_, kind), path in selected.items() if kind == "test.log")
        xml = next(path for (_, kind), path in selected.items() if kind == "test.xml")
        cases = list(ET.parse(xml).getroot().iter("testcase"))
        names = [case.attrib["name"] for case in cases]
        if not names or len(set(names)) != len(names) or any(list(case) for case in cases):
            raise ValueError(f"{name}: invalid positive XML census for {owner}")
        if owner in ANALYZERS:
            output = log.read_text()
            if len(names) != 1 or not all(f"{cpu}...PASS" in output for cpu in ("amd64", "arm64")):
                raise ValueError(f"{name}: missing actual analyzer CPU results for {owner}")
        else:
            required = {"TestTable", "TestFDTable", "TestTaskCPU", "TestInitSignalDiscarded",
                        "TestRunInterruptPullFullStateError"}
            if not required <= set(names):
                raise ValueError(f"{name}: missing required real cases for {owner}")
        checks.append({"owner": owner, "names": names,
                       "xml": report.file_record(xml), "log": report.file_record(log)})
    return checks


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    if config["targets"] != UNITS + ANALYZERS:
        raise ValueError("Unexpected split qualification owner universe")
    arms = config["arms"]
    if [arm["name"] for arm in arms] != ["contracts", "parent-death", "pull-full-state", "exit-status", "combined"]:
        raise ValueError("Unexpected independent split selection")
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"],
               "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    race_options = [value for value in options if value != "--lockfile_mode=error"]
    prepared = invoke("race-dependency-preparation", ["cquery", *race_options, "--config=race",
                      "--lockfile_mode=update", "--output=label", "set(" + " ".join(UNITS) + ")"],
                      "command", targets=[])
    if prepared["status"] != "PASS":
        raise RuntimeError("Race configured dependency preparation failed")
    selected = ["--strategy=TestRunner=local", "--nocache_test_results", "--runs_per_test=1",
                "--flaky_test_attempts=1", "--test_output=all", "--test_timeout=600",
                "--test_sharding_strategy=disabled", "--test_tag_filters=", "--keep_going"]
    record = {"status": "INCOMPLETE", "source": config["source"], "base": config["base"],
              "arms": [], "restored": False}
    try:
        for arm in arms:
            source = arm["source"]
            subprocess.run(["git", "checkout", "--detach", source], check=True, timeout=30)
            if (subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != source
                    or subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"])):
                raise ValueError("Source switch did not reach a clean independent arm")
            for name, expected in arm["matched_inputs"].items():
                path = Path(name)
                body = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
                if hashlib.sha256(body).hexdigest() != expected:
                    raise ValueError(f"{arm['name']}: source input differs: {name}")
            if (not arm["analyzers"] or not set(arm["analyzers"]) <= set(ANALYZERS)
                    or not set(arm["units"]) <= set(UNITS)):
                raise ValueError("Unexpected per-source owning targets")
            current = {"name": arm["name"], "source": source, "product": arm["product"], "checks": []}
            record["arms"].append(current)
            formatted = invoke(arm["name"] + "-goimports", ["run", *options, "--run_in_cwd",
                               "@org_golang_x_tools//cmd/goimports", "--", "-format-only", "-d",
                               *arm["format_files"]], "command", targets=[], source=source)
            stdout = evidence / ("invocation-" + arm["name"] + "-goimports") / "stdout.log"
            if formatted["status"] != "PASS" or stdout.read_bytes():
                raise RuntimeError("Canonical goimports reported a formatting difference")
            phases = [("nogo", arm["analyzers"], [])]
            if arm["units"]:
                phases.extend([("units", arm["units"], []), ("race", arm["units"], ["--config=race"])])
            for suffix, owners, extra in phases:
                name = arm["name"] + "-" + suffix
                args = ["test", *options, *extra, *selected]
                if suffix != "nogo":
                    args.extend(["--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=1"])
                result = invoke(name, [*args, *owners], "test", targets=owners, source=source)
                current["checks"].append({"name": name, "owners": owners, "result": result})
        # Retain every independent native result before checking the combined gate.
        for arm in record["arms"]:
            for check in arm["checks"]:
                check["cases"] = check_reports(evidence, check["name"], check["owners"], check["result"])
        record["status"] = "PASS"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        record["status"] = "FAIL"
        raise
    finally:
        try:
            subprocess.run(["git", "checkout", "--detach", config["source"]], check=True, timeout=30)
            if (subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != config["source"]
                    or subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"])):
                raise ValueError("Failed to restore initial qualification source")
            record["restored"] = True
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            record.update(status="FAIL", restoration_error=str(error))
            raise
        finally:
            report.write_json(evidence / "split-checks.json", record)
