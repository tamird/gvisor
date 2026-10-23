"""Exercise existing Nginx and Empty benchmark consumers at fixed small counts."""
from dataclasses import asdict
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

import qualification_report as report
from image_runtime import prepare_benchmark_helpers, prepared_runtime


def run(invoke, evidence: Path, private: Path) -> None:
    records = {
        "status": "INCOMPLETE",
        "scope": "AMD64 consumer correctness only; no comparative performance or ARM runtime claim",
        "commands": [],
        "consumers": [],
        "responses": [],
    }

    def remaining() -> float:
        seconds = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Benchmark consumers exhausted the work deadline")
        return seconds

    def command(arguments: list[str]) -> str:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=remaining())
        records["commands"].append({"arguments": arguments, "native_exit": result.returncode,
                                    "stdout": result.stdout, "stderr": result.stderr})
        result.check_returncode()
        return result.stdout

    def consumer(phase: str, target: str, runtime: str, pattern: str,
                 names: list[str], iterations: int, units: list[str]) -> None:
        options = [
            "--compilation_mode=opt", "--lockfile_mode=error", "--noannounce_rc",
            "--remote_download_outputs=all", "--build_event_json_file_path_conversion=false",
            "--config=x86_64", "--runs_per_test=1", "--flaky_test_attempts=1",
            "--nocache_test_results", "--test_env=GO_TEST_WRAP_TESTV=1",
            "--nozip_undeclared_test_outputs", "--test_sharding_strategy=disabled",
            "--strategy=TestRunner=local", "--local_test_jobs=1", "--test_timeout=900",
            "--test_arg=-runtime=" + runtime, "--test_arg=-test.v=test2json",
            "--test_filter=^$", "--test_arg=-test.run=^$",
            "--test_arg=-test.bench=" + pattern,
            "--test_arg=-test.benchtime=" + str(iterations) + "x",
            "--test_arg=-test.count=1", "--test_arg=-test-checkpoint=true",
        ]
        row = {"phase": phase, "target": target, "status": "INCOMPLETE",
               "required_benchmarks": names, "iterations": iterations, "units": units}
        records["consumers"].append(row)
        analyzed = invoke(phase + "-analysis", ["test", *options, "--nobuild", target],
                          "analysis", targets=[target])
        if analyzed["status"] != "PASS":
            raise RuntimeError("Benchmark consumer analysis failed: " + phase)
        benchmark_manifest = evidence / (phase + "-declared-benchmarks.json")
        report.write_json(benchmark_manifest, {"owners": {target: [
            {"name": name, "count": 1, "iterations": iterations, "units": units}
            for name in names]}})
        tested = invoke(phase, ["test", *options, target], "test",
                        (evidence / ("invocation-" + phase + "-analysis") / "events.jsonl",),
                        targets=[target], declared_benchmarks=benchmark_manifest)
        row["native_attempts"] = tested["executed"]
        match tested["executed"]:
            case [attempt] if not attempt["cached_locally"] and not attempt["execution_info"].get("cachedRemotely", False):
                row["benchmarks"] = attempt.get("benchmarks", [])
            case _:
                raise ValueError("Benchmark consumer must execute once without a cached result")
        manifest = report.manifest_path(evidence / ("invocation-" + phase) / "events.jsonl")
        _, retained, errors = report.retained_reports(manifest)
        if errors:
            raise ValueError("Cannot authenticate benchmark reports: " + str(errors))
        xmls = [(attempt, path) for (attempt, name), path in retained.items() if name == "test.xml"]
        match xmls:
            case [(attempt, path)] if attempt.owner.label == target:
                counts = report.xml_cases(path)
                row.update(attempt=asdict(attempt), xml=report.file_record(path), counts=counts,
                           xml_names=[case.get("name") for case in ET.parse(path).getroot().iter("testcase")])
            case _:
                raise ValueError("Benchmark XML owner set differs")
        logs = [(attempt, path) for (attempt, name), path in retained.items() if name == "test.log"]
        match logs:
            case [(attempt, path)] if attempt.owner.label == target:
                row["benchmark_log"] = report.file_record(path)
            case _:
                raise ValueError("Expected the selected owner's one retained benchmark log")
        # Benchmarks need not create named XML cases. The existing reporter
        # matches every declared native benchmark name, count, iteration and unit.
        if tested["status"] != "PASS" or counts["failed"] or counts["skipped"]:
            raise RuntimeError("Existing benchmark consumer failed or skipped: " + phase)
        row["status"] = "PASS"

    def response_contract(runtime: str, image: str, filesystem: str, directory: Path) -> None:
        # These are the exact two commands selected by runNginx. Check response
        # bytes separately: Hey's timing parser does not enforce HTTP status.
        server_command = (["sh", "-c", "mkdir -p /tmp/html && cp -a /local/* /tmp/html && nginx -c /etc/nginx/nginx.conf"]
                          if filesystem == "tmpfs" else ["nginx", "-c", "/etc/nginx/nginx_gofer.conf"])
        name = runtime + "-response-" + uuid.uuid4().hex
        row = {"filesystem": filesystem, "container": name, "status": "INCOMPLETE", "responses": []}
        records["responses"].append(row)
        try:
            command(["docker", "run", "--detach", "--name=" + name, "--runtime=" + runtime,
                     image, *server_command])
            inspected = json.loads(command(["docker", "inspect", name]))
            match inspected:
                case [{"NetworkSettings": {"IPAddress": address}, "Image": actual_image}] if address and actual_image == image:
                    pass
                case _:
                    raise ValueError("Benchmark server image or IPv4 address differs")
            ready_deadline = time.monotonic() + min(30, remaining())
            while True:
                connection = http.client.HTTPConnection(address, 80, timeout=min(5, remaining()))
                try:
                    connection.request("GET", "/latin1k.txt", headers={"Accept-Encoding": "identity"})
                    response = connection.getresponse()
                    if response.status == 200 and response.read(2048) == bytes(1024):
                        break
                except (OSError, http.client.HTTPException):
                    if time.monotonic() >= ready_deadline:
                        raise
                finally:
                    connection.close()
                if time.monotonic() >= ready_deadline:
                    raise TimeoutError("Nginx did not serve the selected payload within 30 seconds")
                time.sleep(0.1)
            for path, expected_status, expected_bytes in (("latin1k.txt", 200, 1024),
                                                          ("latin1024k.txt", 200, 1024 * 1024),
                                                          ("notfound", 404, None)):
                connection = http.client.HTTPConnection(address, 80, timeout=min(10, remaining()))
                try:
                    connection.request("GET", "/" + path, headers={"Accept-Encoding": "identity"})
                    response = connection.getresponse()
                    body = response.read(1024 * 1024 + 1)
                    headers = {key.lower(): value for key, value in response.getheaders()}
                    if response.status != expected_status or len(body) > 1024 * 1024:
                        raise ValueError("Nginx response status or size differs: " + path)
                    if headers.get("content-encoding") not in (None, "identity"):
                        raise ValueError("Nginx compressed an identity-only response")
                    if expected_bytes is not None and body != bytes(expected_bytes):
                        raise ValueError("Nginx did not serve the exact zero-filled payload: " + path)
                    if expected_bytes is None and (not body or headers.get("content-type", "").split(";", 1)[0] != "text/html"):
                        raise ValueError("Nginx omitted its HTTP error response")
                    row["responses"].append({"path": path, "status": response.status, "headers": headers,
                                             "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
                finally:
                    connection.close()
            row["status"] = "PASS"
        finally:
            try:
                logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True, timeout=30)
                log_path = directory / ("response-" + filesystem + ".log")
                log_path.write_text(logs.stdout + logs.stderr)
                row["logs"] = report.file_record(log_path)
            finally:
                cleanup = subprocess.run(["docker", "rm", "--force", name], capture_output=True, text=True, timeout=30)
                row["cleanup"] = {"native_exit": cleanup.returncode, "stdout": cleanup.stdout, "stderr": cleanup.stderr}
                if cleanup.returncode and "No such container" not in cleanup.stderr:
                    raise RuntimeError("Failed to remove the owned Nginx response container")

    try:
        records["containers_before"] = sorted(command(["docker", "ps", "--all", "--quiet", "--no-trunc"]).splitlines())
        for image_name, contents_name, runtime in (
                ("benchmarks/nginx", "benchmarks-nginx-contents.json", "qualification-benchmark-nginx"),
                ("basic/busybox", "basic-busybox-contents.json", "qualification-benchmark-empty")):
            contents_path = evidence / contents_name
            contents = report.read_json(contents_path)
            match [row for row in contents["architectures"] if row["architecture"] == "amd64"]:
                case [architecture]:
                    image = "sha256:" + architecture["config_sha256"]
                case _:
                    raise ValueError("Expected one inspected AMD64 runtime image")
            directory = evidence / runtime
            directory.mkdir()
            configuration = {"runtime_name": runtime, "image_name": image_name,
                             "runtime_flags": ["--platform=systrap", "--network=sandbox"], "contents": str(contents_path)}
            with prepared_runtime(directory / "runtime-preparation.json", private / runtime, configuration):
                if image_name == "benchmarks/nginx":
                    for filesystem in ("tmpfs", "bind"):
                        response_contract(runtime, image, filesystem, directory)
                    configuration = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
                    records["benchmark_helpers"] = prepare_benchmark_helpers(
                        evidence / "benchmark-helper-images.json", private / "benchmark-helper-images",
                        configuration["benchmark_utility"])
                    consumer("nginx-benchmark-serving", "//test/benchmarks/network:nginx_test", runtime,
                             r"^BenchmarkNginxDocSize$/^filesize\.(1Kb|1Mb|notfound)$/^concurrency\.1$/^filesystem\.(tmpfs|bind)$",
                             ["BenchmarkNginxDocSize/filesize." + size + "/concurrency.1/filesystem." + filesystem
                              for size in ("1Kb", "1Mb", "notfound") for filesystem in ("tmpfs", "bind")],
                             100, ["ns/op", "average_latency.s", "requests_per_second.QPS"])
                    consumer("nginx-benchmark-lifecycle", "//test/benchmarks/base:startup_test", runtime,
                             r"^Benchmark(Startup|Checkpoint|Restore)Nginx$",
                             ["Benchmark" + operation + "Nginx" for operation in ("Startup", "Checkpoint", "Restore")],
                             1, ["ns/op"])
                else:
                    consumer("empty-benchmark-lifecycle", "//test/benchmarks/base:startup_test", runtime,
                             r"^Benchmark(Startup|Checkpoint|Restore)Empty$",
                             ["Benchmark" + operation + "Empty" for operation in ("Startup", "Checkpoint", "Restore")],
                             1, ["ns/op"])
                    for measurement, unit in (("size", "average_container_size.bytes"),
                                              ("usage", "average_container_memory_usage.bytes")):
                        consumer("empty-benchmark-" + measurement, "//test/benchmarks/base:" + measurement + "_test",
                                 runtime, r"^BenchmarkSizeEmpty$", ["BenchmarkSizeEmpty"], 1, ["ns/op", unit])
        records["benchmark_client_images"] = {}
        for name in ("hey", "util"):
            helper = records["benchmark_helpers"][name]
            match json.loads(command(["docker", "image", "inspect", helper["name"]])):
                case [image] if image["Id"] == helper["image"]["Id"]:
                    records["benchmark_client_images"][name] = image
                case other:
                    raise ValueError(f"Benchmark helper identity changed: {name}: {other}")
        records["containers_after"] = sorted(command(["docker", "ps", "--all", "--quiet", "--no-trunc"]).splitlines())
        if records["containers_after"] != records["containers_before"]:
            raise RuntimeError("Benchmark consumers left containers behind")
        records["status"] = "PASS"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, http.client.HTTPException) as error:
        records.update(status="FAIL", error=str(error))
        raise
    finally:
        report.write_json(evidence / "nginx-benchmark-runtime-results.json", records)
