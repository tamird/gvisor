"""Qualify lazy advisory-lock storage with existing units and syscall owners."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import qualification_report as report


UNIT = "//pkg/sentry/fsimpl/lock:lock_test"
ANALYZERS = ["//pkg/sentry/fsimpl/lock:lock_nogo", UNIT + "_nogo"]
SYSCALLS = ["//test/syscalls:" + binary + "_runsc_systrap_" + mode
            for binary in ("fcntl_test", "flock_test")
            for mode in ("directfs", "shared")]
SAVES = ["//test/syscalls:" + binary + "_runsc_systrap_directfs_save"
         for binary in ("fcntl_test", "flock_test")]
ARM = [UNIT, SYSCALLS[0], SYSCALLS[2]]
UNIT_CASES = {"TestComputeRange", "TestCanLockEmpty", "TestCanLock", "TestSetLock", "TestUnlock", "TestUnusedFileLocks"}
SAVE_CASES = ["FcntlLockTest.TestOFDBasicLock", "FlockTest.TestDupFdCloseRelease"]


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    if config["targets"] != [UNIT, *ANALYZERS, *SYSCALLS, *SAVES]:
        raise ValueError(f"Unexpected lock owner selection: {config['targets']}")
    record = {"source": config["source"], "product": config["product"],
              "status": "INCOMPLETE", "checks": [], "errors": [],
              "architecture_scope": "AMD64 runtime; ARM64 compilation and both-CPU Nogo only"}
    try:
        for path, expected in config["matched_inputs"].items():
            actual = subprocess.check_output(["git", "show", config["source"] + ":" + path])
            if hashlib.sha256(actual).hexdigest() != expected:
                raise ValueError(f"Frozen source input differs: {path}")
        options = ["--compilation_mode=opt", "--lockfile_mode=error", *config["consumer_options"],
                   "--remote_download_outputs=all", "--noannounce_rc", "--keep_going"]
        prepared = invoke("race-dependency-preparation", ["cquery", "--config=x86_64", "--config=race",
                          *[option for option in options if option != "--lockfile_mode=error"],
                          "--lockfile_mode=update", "--output=label", UNIT], "command", targets=[])
        record["checks"].append({"name": "race-dependency-preparation", "result": prepared})
        if prepared["status"] != "PASS":
            raise RuntimeError("The exact race graph failed dependency preparation")
        formatted = invoke("goimports", ["run", "--config=x86_64", *options, "--run_in_cwd",
                           "@org_golang_x_tools//cmd/goimports", "--", "-format-only", "-d",
                           "pkg/sentry/fsimpl/lock/lock.go", "pkg/sentry/fsimpl/lock/lock_test.go"],
                           "command", targets=[])
        record["checks"].append({"name": "goimports", "result": formatted})
        built = invoke("arm64-compile", ["build", "--config=aarch64", *options, *ARM], "build", targets=ARM)
        record["checks"].append({"name": "arm64-compile", "result": built})
        testing = ["--config=x86_64", *options, "--strategy=TestRunner=local", "--nocache_test_results",
                   "--runs_per_test=1", "--flaky_test_attempts=1", "--test_output=all",
                   "--test_sharding_strategy=disabled", "--test_timeout=600", "--test_tag_filters="]
        phases = [("lock-unit", [UNIT], ["--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=1"]),
                  ("lock-race", [UNIT], ["--config=race", "--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=1"]),
                  ("lock-nogo", ANALYZERS, [])]
        for owner in SYSCALLS:
            binary = "fcntl" if "fcntl_test" in owner else "flock"
            phases.append((binary + "-" + owner.rsplit("_", 1)[1], [owner],
                           ["--test_env=GTEST_FILTER=" + ":".join(config["cases"][binary])]))
        for owner, case in zip(SAVES, SAVE_CASES, strict=True):
            phases.append(("save-ofd" if "fcntl" in owner else "save-bsd", [owner], ["--test_env=GTEST_FILTER=" + case]))
        # Finish every native test phase before interpreting reports, so a
        # reporting error does not erase independent architecture/test evidence.
        for name, owners, extra in phases:
            result = invoke(name, ["test", *testing, *extra, *owners], "test", targets=owners)
            record["checks"].append({"name": name, "result": result, "reports": []})
        for check in record["checks"]:
            result = check["result"]
            if result["status"] != "PASS":
                record["errors"].append(f"{check['name']}: native verdict {result['status']}")
        if (evidence / "invocation-goimports/stdout.log").read_bytes():
            record["errors"].append("Canonical Go import formatting differs")
        seen_paths: set[Path] = set()
        for name, owners, _ in phases:
            check = next(row for row in record["checks"] if row["name"] == name)
            result = check["result"]
            try:
                counts = result["case_counts"]
                if counts["failed"] or counts["skipped"] or not counts["passed"]:
                    raise ValueError(f"Expected positive cases without failures/skips: {counts}")
                attempts = result["executed"]
                if len(attempts) != len(owners) or {a["owner"]["label"] for a in attempts} != set(owners):
                    raise ValueError(f"Executed owners differ: {attempts}")
                expected_attempts = set()
                for attempt in attempts:
                    if (attempt["run"], attempt["shard"], attempt["attempt"]) != (1, 1, 1) or attempt["cached_locally"] or attempt["execution_info"].get("cachedRemotely", False) or attempt["execution_info"].get("strategy") != "local":
                        raise ValueError(f"Expected one uncached hosted-local attempt: {attempt}")
                    expected_attempts.add((attempt["owner"]["label"], attempt["owner"]["configuration"], 1, 1, 1))
                _, reports, errors = report.retained_reports(report.manifest_path(evidence / ("invocation-" + name) / "events.jsonl"))
                actual_attempts = {(a.owner.label, a.owner.configuration, a.run, a.shard, a.attempt)
                                   for (a, kind) in reports if kind == "test.xml"}
                if errors or actual_attempts != expected_attempts or len(reports) != 2 * len(owners):
                    raise ValueError(f"Missing, aliased or unexpected retained reports: {errors}, {actual_attempts}")
                for (attempt, kind), path in reports.items():
                    if path in seen_paths:
                        raise ValueError(f"Repeated retained report path: {path}")
                    seen_paths.add(path)
                    if kind != "test.xml":
                        continue
                    log_path = reports[(attempt, "test.log")]
                    log = log_path.read_text(errors="strict")
                    cases = list(ET.parse(path).getroot().iter("testcase"))
                    if any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
                        raise ValueError("XML contains failed, skipped or errored cases")
                    names = [case.attrib["name"] for case in cases]
                    details = {"owner": attempt.owner.label, "configuration": attempt.owner.configuration,
                               "xml": report.file_record(path), "log": report.file_record(log_path), "names": names}
                    if name in ("lock-unit", "lock-race"):
                        if not UNIT_CASES <= set(names):
                            raise ValueError(f"Missing lock unit cases: {UNIT_CASES - set(names)}")
                    elif name == "lock-nogo":
                        if len(cases) != 1 or any(marker not in log for marker in ("amd64...PASS", "arm64...PASS")):
                            raise ValueError("Missing one positive Nogo wrapper or both architecture markers")
                    else:
                        names = [case.attrib["classname"] + "." + case.attrib["name"] for case in cases]
                        wanted = ([SAVE_CASES[0]] if name == "save-ofd" else [SAVE_CASES[1]] if name == "save-bsd"
                                  else config["cases"][name.split("-", 1)[0]])
                        if len(names) != len(wanted) or set(names) != set(wanted):
                            raise ValueError(f"Syscall cases differ: {names}; expected {wanted}")
                        details["names"] = names
                        if name.startswith("save-"):
                            restores = [line for line in log.splitlines()
                                        if "Executing: [" in line and " restore " in line and "--image-path " in line and "--bundle " in line]
                            minimum = 4 if name == "save-ofd" else 5
                            if len(restores) < minimum or not all("-TESTONLY-autosave-image-path" in line for line in restores):
                                raise ValueError(f"Expected at least {minimum} actual cooperative restores, got {len(restores)}")
                            details["restore_commands"] = restores
                            details["restore_count"] = len(restores)
                            details["scope"] = "Single named test; successful native runner returns only after each logged restore succeeds. Matchers save after acquiring/observing the held lock; no blocked-waiter checkpoint claim."
                    check["reports"].append(details)
            except (KeyError, OSError, ValueError, TypeError, ET.ParseError) as error:
                record["errors"].append(f"{name}: {error}")
        if record["errors"]:
            raise RuntimeError("; ".join(record["errors"]))
        record["status"] = "PASS"
    except Exception as error:
        record["status"] = "FAIL"
        record["error"] = str(error)
        raise
    finally:
        report.write_json(evidence / "lock-behavior.json", record)
