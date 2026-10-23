"""Run the existing metric owner, focused race cases, and native Nogo."""
import json
import os
from pathlib import Path
import shlex
import xml.etree.ElementTree as ET

import qualification_report as report


UNIT = "//pkg/metric:metric_test"
NOGO = "//pkg/metric:metric_nogo"
ROOTS = {
    "TestVerifyName", "TestInitialize", "TestDisable", "TestEmitMetricUpdate",
    "TestMetricUpdateStageTiming", "TestTimerMetric", "TestBucketer",
    "TestBucketerPanics", "TestFieldMapperWithFields", "TestFieldMapperNoFields",
    "TestFieldValueUniqueness", "TestFieldMapperMustUseSameValuePointer",
    "TestMetricProfiling",
}
PROFILING = {"TestMetricProfiling"} | {
    "TestMetricProfiling/" + name for name in (
        "simple_single_metric", "simple_single_metric,_lossy_writer",
        "single_metric_exceeds_snapshot_buffer", "multiple_metrics",
        "single_metric_with_1_field", "single_metric_with_multiple_fields",
        "multiple_metrics_and_one_has_fields", "mismatched_names",
    )
}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    if config["targets"] != [UNIT, NOGO]:
        raise ValueError(f"Unexpected metric targets: {config['targets']}")
    record = {"source": config["source"], "status": "INCOMPLETE", "checks": []}
    try:
        formatter = shlex.join([
            "bash", "-c",
            'set -eu; export PATH="$(dirname "$1"):$PATH"; '
            'cd "$BUILD_WORKSPACE_DIRECTORY"; exec bash tools/lint.sh gofmt',
            "--",
        ])
        formatted = invoke("gofmt", [
            "run", "--lockfile_mode=error", "--noannounce_rc",
            "--run_under=" + formatter, "@io_bazel_rules_go//go:go",
        ], "command", targets=[])
        if formatted["status"] != "PASS":
            raise RuntimeError("Canonical Go formatting failed")
        options = [
            "--compilation_mode=opt", "--config=x86_64", "--lockfile_mode=error",
            "--extra_toolchains=@llvm//toolchain:all", "--remote_download_outputs=all",
            "--noannounce_rc", "--keep_going", "--strategy=TestRunner=local",
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_output=all",
            "--test_sharding_strategy=disabled", "--test_timeout=300",
        ]
        for name, owner, runs, extra in (
            ("metric-unit", UNIT, 1, []),
            ("metric-profiling-race", UNIT, 8, ["--config=race", "--test_filter=^TestMetricProfiling$", "--local_test_jobs=4"]),
            ("metric-nogo", NOGO, 1, []),
        ):
            reporting = ["--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=1"] if owner == UNIT else []
            result = invoke(name, ["test", *options, *reporting,
                                  "--runs_per_test=" + str(runs), *extra, owner],
                            "test", targets=[owner])
            check = {"name": name, "owner": owner, "result": result, "case_names": []}
            record["checks"].append(check)
            if result["status"] != "PASS" or result["case_counts"]["failed"] or result["case_counts"]["skipped"]:
                raise RuntimeError(f"{name}: expected PASS without skipped or failed cases")
            attempts = result["executed"]
            if len(attempts) != runs or {a["run"] for a in attempts} != set(range(1, runs + 1)):
                raise RuntimeError(f"{name}: expected {runs} distinct native attempts, got {attempts}")
            for attempt in attempts:
                if (attempt["owner"]["label"] != owner or attempt["attempt"] != 1
                        or attempt["cached_locally"] or attempt["execution_info"].get("cachedRemotely", False)
                        or attempt["execution_info"].get("strategy") != "local"):
                    raise RuntimeError(f"{name}: unexpected owner, cache, retry, or strategy: {attempt}")
            if owner == NOGO:
                if result["case_counts"] != {"failed": 0, "passed": 1, "skipped": 0}:
                    raise RuntimeError(f"Expected one native both-CPU Nogo wrapper: {result['case_counts']}")
                continue
            events = evidence / ("invocation-" + name) / "events.jsonl"
            _, reports, errors = report.retained_reports(report.manifest_path(events))
            xml_reports = [(attempt, path) for (attempt, kind), path in reports.items() if kind == "test.xml"]
            if errors or len(xml_reports) != runs:
                raise RuntimeError(f"{name}: expected {runs} authenticated XML reports; errors={errors}")
            for attempt, path in sorted(xml_reports):
                names = [case.attrib["name"] for case in ET.parse(path).getroot().iter("testcase")]
                expected = PROFILING if name == "metric-profiling-race" else ROOTS | PROFILING
                if not expected <= set(names) or len(names) != len(set(names)):
                    raise RuntimeError(f"{name}: missing or duplicate actual cases: {names}; required={sorted(expected)}")
                if name == "metric-profiling-race" and set(names) != PROFILING:
                    raise RuntimeError(f"Unexpected focused race selection: {names}")
                check["case_names"].append({"run": attempt.run, "names": names, "xml": report.file_record(path)})
        record["status"] = "PASS"
    except Exception:
        record["status"] = "FAIL"
        raise
    finally:
        report.write_json(evidence / "metric-checks.json", record)
