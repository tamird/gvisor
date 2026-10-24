"""Exercise the unchanged Redis database consumer with the pinned fixture."""
import json
import os
from pathlib import Path
import subprocess
import uuid

import qualification_report as report
from image_runtime import materialize_retained_release, prepared_runtime


def run(invoke, evidence: Path, private: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    target = "//test/benchmarks/database:redis_test"
    phase = "redis-benchmark-consumer"
    records = {"source": config["source"], "status": "INCOMPLETE", "commands": [],
               "runtime_source": config["runtime_supplier"]["source"]}

    def inventory() -> list[str]:
        arguments = ["docker", "ps", "-aq", "--no-trunc"]
        row = {"arguments": arguments, "native_exit": None}
        records["commands"].append(row)
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
        row.update(native_exit=result.returncode, stdout=result.stdout, stderr=result.stderr)
        result.check_returncode()
        return sorted(result.stdout.split())

    before = None
    try:
        before = inventory()
        runtime_directory = private / "redis-runtime"
        retained = materialize_retained_release(config["runtime_supplier"], runtime_directory, evidence, "redis")
        runtime = "qualification-redis-benchmark-" + uuid.uuid4().hex
        records["runtime_name"] = runtime
        setup = {"runtime_name": runtime, "image_name": "benchmarks/redis",
                 "runtime_flags": ["--platform=systrap", "--network=sandbox"],
                 "contents": str(evidence / "redis-contents.json"),
                 "retained_release_files": retained, "release_supplier": config["runtime_supplier"]}
        with prepared_runtime(evidence / "redis-runtime-preparation.json", runtime_directory, setup):
            options = [*config["consumer_options"], "--compilation_mode=opt", "--config=x86_64",
                       "--lockfile_mode=error", "--noannounce_rc", "--remote_download_outputs=all",
                       "--build_event_json_file_path_conversion=false", "--nozip_undeclared_test_outputs",
                       "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                       "--test_sharding_strategy=disabled", "--strategy=TestRunner=local", "--local_test_jobs=1",
                       "--test_timeout=300", "--test_env=GO_TEST_WRAP_TESTV=0",
                       "--test_arg=-runtime=" + runtime, "--test_arg=-test.v=false",
                       "--test_filter=^$", "--test_arg=-test.run=^$",
                       "--test_arg=-test.bench=^BenchmarkRedis$", "--test_arg=-test.benchtime=10000x",
                       "--test_arg=-test.count=1"]
            analyzed = invoke(phase + "-analysis", ["test", *options, "--nobuild", target], "analysis", targets=[target])
            if analyzed["status"] != "PASS":
                raise RuntimeError("Redis benchmark owner analysis failed")
            declared = evidence / "redis-declared-benchmarks.json"
            report.write_json(declared, {"owners": {target: config["benchmarks"]}})
            tested = invoke(phase, ["test", *options, target], "test",
                            (evidence / ("invocation-" + phase + "-analysis/events.jsonl"),),
                            targets=[target], declared_benchmarks=declared)
            records["native_attempts"] = tested["executed"]
            manifest = report.manifest_path(evidence / ("invocation-" + phase) / "events.jsonl")
            _, retained_reports, errors = report.retained_reports(manifest)
            if errors:
                raise ValueError("Cannot authenticate Redis reports: " + str(errors))
            for kind in ("xml", "log"):
                match [(attempt, path) for (attempt, name), path in retained_reports.items()
                       if name == "test." + kind]:
                    case [(attempt, path)] if attempt.owner.label == target:
                        records[kind] = report.file_record(path)
                        if kind == "xml":
                            records["counts"] = report.xml_cases(path)
                    case _:
                        raise ValueError("Expected one Redis benchmark report: " + kind)
            if tested["status"] != "PASS" or records["counts"]["failed"] or records["counts"]["skipped"]:
                raise RuntimeError("Existing Redis benchmark consumer failed")
            if inventory() != before:
                raise RuntimeError("Redis benchmark left containers after completion")
        records["status"] = "PASS"
    finally:
        completed = records["status"]
        records["status"] = "FAIL"
        try:
            after = inventory()
            records["container_inventory"] = {"before": before, "after": after}
            if before is None or after != before:
                raise RuntimeError("Redis qualification changed container inventory")
            records["cleanup"] = "PASS"
            records["status"] = completed
        finally:
            report.write_json(evidence / "redis-runtime-results.json", records)
