"""Run the existing Bun spawn benchmark once with the inspected image."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time
import urllib.request
import uuid

import qualification_report as report
from image_runtime import prepared_runtime


def materialize_runtime(supplier: dict, directory: Path, evidence: Path) -> dict:
    """Verify the retained release owner before fetching its seven runtime files."""
    records = {"status": "INCOMPLETE", "supplier": supplier, "files": {}}

    def fetch(item: dict, destination: Path) -> None:
        remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Retained runtime retrieval exhausted the work deadline")
        request = urllib.request.Request("https://app.buildbuddy.io/api/v1/GetFile",
            data=json.dumps({"uri": item["uri"]}).encode(),
            headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        with urllib.request.urlopen(request, timeout=min(180, remaining)) as response:
            data = response.read(item["bytes"] + 1)
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("Retained runtime supplier identity mismatch")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

    try:
        archive_path = directory.parent / "runtime-supplier.tar.gz"
        fetch(supplier["bundle"], archive_path)
        with tarfile.open(archive_path) as archive:
            names = [entry.name for entry in archive if entry.isfile()]
            if len(names) != len(set(names)):
                raise ValueError("Duplicate retained runtime evidence member")
            phase = "invocation-paired-harness/"
            verdict = json.load(archive.extractfile(phase + "verdict.json"))
            if verdict["source"] != supplier["source"] or verdict["status"] != "PASS" or verdict["native_exit"] != 0:
                raise ValueError("Retained runtime build lacks exact-source native success")
            owners = [row for row in verdict["expected"] if row["label"] == "//:release"]
            if owners != [supplier["owner"]]:
                raise ValueError("Retained runtime configured release owner differs")
            events = evidence / "bun-runtime-supplier-events.jsonl"
            events.write_bytes(archive.extractfile(phase + "events.jsonl").read())
            report.write_json(evidence / "bun-runtime-supplier-verdict.json", verdict)
        owner = report.Owner(supplier["owner"]["label"], supplier["owner"]["configuration"])
        outputs = report.advertised_build_outputs(events, {(owner, "default")})
        by_name = {row["name"].split("/release/", 1)[1]: row for row in outputs}
        expected = {"runsc", "gvisor-bin/checkpointgofer", "gvisor-bin/runsc-metric-server",
                    "gvisor-bin/runsc-fd-parking", "gvisor-bin/gvisor-sentry-prewarmer",
                    "gvisor-bin/gvisor-proc-umounter", "gvisor-bin/gvisor_sentry"}
        if len(outputs) != 8 or set(by_name) != expected | {"containerd-shim-runsc-v1"} or set(supplier["files"]) != expected:
            raise ValueError("Retained release output set differs")
        records["release_outputs"] = outputs
        for name, item in supplier["files"].items():
            if any(by_name[name][key] != item[key] for key in ("bytes", "sha256")):
                raise ValueError("Retained runtime file differs from its default-BEP output: " + name)
            # The BEP advertises materialized files. CAS availability is checked
            # here, using their authenticated content digests, without fallback.
            artifact = {**item, "uri": f"bytestream://remote.buildbuddy.io/blobs/{item['sha256']}/{item['bytes']}"}
            path = directory / name
            fetch(artifact, path)
            path.chmod(0o755)
            records["files"][name] = {**report.file_record(path), "uri": artifact["uri"]}
        records["status"] = "PASS"
        return {name: {key: row[key] for key in ("bytes", "sha256")} for name, row in records["files"].items()}
    finally:
        report.write_json(evidence / "bun-runtime-supplier.json", records)


def run(invoke, evidence: Path, private: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    result = {"source": config["source"], "status": "INCOMPLETE", "commands": [],
              "scope": "One AMD64 functional benchmark; no comparative performance or ARM runtime claim"}
    runtime = "qualification-bun-" + uuid.uuid4().hex
    setup = {"runtime_name": runtime, "image_name": "benchmarks/bun-spawn",
             "runtime_flags": ["--platform=systrap", "--network=sandbox"],
             "contents": str(evidence / "bun-contents.json")}

    def command(arguments: list[str], timeout: float) -> str:
        row = {"arguments": arguments, "native_exit": None}
        result["commands"].append(row)
        proc = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
        row.update(native_exit=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
        proc.check_returncode()
        return proc.stdout

    before = sorted(command(["docker", "ps", "-aq", "--no-trunc"], 30).split())
    try:
        runtime_directory = private / "bun-runtime"
        retained = materialize_runtime(config["runtime_supplier"], runtime_directory, evidence)
        setup.update(retained_release_files=retained, release_supplier=config["runtime_supplier"])
        result["runtime_source"] = config["runtime_supplier"]["source"]
        with prepared_runtime(evidence / "bun-runtime-preparation.json", runtime_directory, setup) as prepared:
            files = {str(Path(row["path"]).relative_to(runtime_directory)): row for row in prepared["runtime_files"]}
            if set(files) != set(retained) or any(any(files[name][key] != item[key] for key in ("bytes", "sha256")) for name, item in retained.items()):
                raise ValueError("Installed runtime differs from the retained release")
            target = "//test/benchmarks/base:bun_spawn_test"
            options = ["--compilation_mode=opt", "--config=x86_64", "--lockfile_mode=error",
                       "--noannounce_rc", "--remote_download_outputs=all",
                       "--build_event_json_file_path_conversion=false", "--nozip_undeclared_test_outputs",
                       "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                       "--test_sharding_strategy=disabled", "--strategy=TestRunner=local", "--local_test_jobs=1",
                       "--test_timeout=600", "--test_env=GO_TEST_WRAP_TESTV=0",
                       "--test_arg=-runtime=" + runtime, "--test_arg=-test.v=false",
                       "--test_filter=^$", "--test_arg=-test.run=^$",
                       "--test_arg=-test.bench=^BenchmarkBunSpawn$", "--test_arg=-test.benchtime=100x",
                       "--test_arg=-test.count=1"]
            declared = evidence / "bun-declared-benchmarks.json"
            report.write_json(declared, {"owners": {target: [
                {"name": "BenchmarkBunSpawn", "count": 1, "iterations": 100, "units": ["ns/op"]}]}})
            analyzed = invoke("bun-consumer-analysis", ["test", *options, "--nobuild", target], "analysis", targets=[target])
            if analyzed["status"] != "PASS":
                raise RuntimeError("Bun benchmark owner analysis failed")
            tested = invoke("bun-consumer", ["test", *options, target], "test",
                            (evidence / "invocation-bun-consumer-analysis/events.jsonl",), targets=[target], declared_benchmarks=declared)
            if tested["status"] != "PASS" or len(tested["executed"]) != 1 or any(
                    row["cached_locally"] or row["execution_info"].get("cachedRemotely", False) for row in tested["executed"]):
                raise ValueError("Bun benchmark did not pass once without a cached result")
            _, retained, errors = report.retained_reports(report.manifest_path(evidence / "invocation-bun-consumer/events.jsonl"))
            if errors:
                raise ValueError("Cannot authenticate Bun benchmark reports: " + str(errors))
            xmls = [(attempt, path) for (attempt, name), path in retained.items() if name == "test.xml"]
            logs = [(attempt, path) for (attempt, name), path in retained.items() if name == "test.log"]
            match xmls, logs:
                case [(attempt, xml)], [(log_attempt, log)] if attempt == log_attempt and attempt.owner.label == target:
                    pass
                case _:
                    raise ValueError("Expected one matching Bun benchmark XML/log owner")
            counts = report.xml_cases(xml)
            if counts["failed"] or counts["skipped"]:
                raise ValueError("Bun benchmark XML contains failure or skip")
            result["consumer"] = {"owner": target, "attempt": asdict(attempt), "xml": report.file_record(xml),
                                  "log": report.file_record(log), "counts": counts, "native_attempts": tested["executed"],
                                  "benchmarks": tested["executed"][0].get("benchmarks", [])}
            # The native benchmark above exercises the original shell/entrypoint
            # setup and /bin/true calls. This command records the selected Bun version.
            name = runtime + "-version"
            try:
                remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Bun version observation exhausted the work deadline")
                version = command(["docker", "run", "--rm", "--name=" + name, "--runtime=" + runtime,
                                   prepared["image"]["id"], "bun", "--version"], min(30, remaining)).strip()
                if version != "1.4.3":
                    raise ValueError("Actual Bun version differs from the pinned fixture: " + version)
                result["version"] = version
            finally:
                cleanup = subprocess.run(["docker", "rm", "--force", name], capture_output=True, text=True, timeout=30)
                result["version_cleanup"] = {"native_exit": cleanup.returncode, "stdout": cleanup.stdout, "stderr": cleanup.stderr}
                if cleanup.returncode and "No such container" not in cleanup.stderr:
                    raise RuntimeError("Cannot remove the owned Bun version container")
        result["status"] = "PASS"
    finally:
        completed = result["status"]
        result["status"] = "FAIL"
        try:
            after = sorted(command(["docker", "ps", "-aq", "--no-trunc"], 30).split())
            result["container_inventory"] = {"before": before, "after": after}
            if after != before:
                raise RuntimeError("Bun qualification changed container inventory")
            result["cleanup"] = "PASS"
            result["status"] = completed
        finally:
            report.write_json(evidence / "bun-runtime-results.json", result)
