"""Exercise the existing Apache image, containerd and benchmark consumers."""
from dataclasses import asdict
import gzip
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
from image_runtime import prepared_runtime


def run(invoke, evidence: Path, private: Path) -> None:
    records = {"status": "INCOMPLETE", "scope": "AMD64 runtime; ARM64 image/configure inspection only", "commands": [], "consumers": []}
    scripts = Path(__file__).parent
    python = ["run", "--lockfile_mode=error", "--noannounce_rc", "@rules_python//python/bin:python", "--"]

    def remaining() -> float:
        seconds = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Apache runtime checks exhausted the work deadline")
        return seconds

    def command(arguments: list[str]) -> str:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=remaining())
        row = {"arguments": arguments, "native_exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        records["commands"].append(row)
        result.check_returncode()
        return result.stdout

    def consumer(phase: str, target: str, runtime: str, pattern: str, required: set[str], extra: list[str] | None = None) -> dict:
        options = ["--compilation_mode=opt", "--lockfile_mode=error", "--noannounce_rc",
                   "--remote_download_outputs=all", "--build_event_json_file_path_conversion=false",
                   "--config=x86_64", "--runs_per_test=1", "--flaky_test_attempts=1",
                   "--nocache_test_results", "--test_env=GO_TEST_WRAP_TESTV=1",
                   "--nozip_undeclared_test_outputs", "--test_sharding_strategy=disabled",
                   "--strategy=TestRunner=local", "--local_test_jobs=1", "--test_timeout=900",
                   "--test_arg=-runtime=" + runtime, "--test_arg=-test.v=test2json",
                   "--test_filter=" + pattern, "--test_arg=-test.run=" + pattern, *(extra or [])]
        row = {"phase": phase, "target": target, "required_cases": sorted(required), "status": "INCOMPLETE"}
        records["consumers"].append(row)
        analyzed = invoke(phase + "-analysis", ["test", *options, "--nobuild", target], "analysis", targets=[target])
        if analyzed["status"] != "PASS":
            raise RuntimeError("Apache consumer analysis failed: " + phase)
        benchmark_manifest = None
        if not required:
            benchmark_manifest = evidence / "httpd-declared-benchmarks.json"
            report.write_json(benchmark_manifest, {"owners": {target: [
                {"name": "BenchmarkHttpd/filesize." + size + "/concurrency." + str(concurrency),
                 "count": 1, "iterations": 100,
                 "units": ["ns/op", "average_latency.s", "requests_per_second.QPS"]}
                for size in ("1Kb", "1Mb", "notfound") for concurrency in (1, 25)]}})
        tested = invoke(phase, ["test", *options, target], "test",
                        (evidence / ("invocation-" + phase + "-analysis") / "events.jsonl",), targets=[target],
                        declared_benchmarks=benchmark_manifest)
        row["native_attempts"] = tested["executed"]
        if len(tested["executed"]) != 1 or any(attempt["cached_locally"] or attempt["execution_info"].get("cachedRemotely", False)
                                              for attempt in tested["executed"]):
            raise ValueError("Apache consumer must execute once without a cached result")
        manifest = report.manifest_path(evidence / ("invocation-" + phase) / "events.jsonl")
        _, retained, errors = report.retained_reports(manifest)
        if errors:
            raise ValueError("Cannot authenticate Apache consumer reports: " + str(errors))
        xmls = [(attempt, path) for (attempt, name), path in retained.items() if name == "test.xml"]
        match xmls:
            case [(attempt, path)] if attempt.owner.label == target:
                names = [case.get("name") for case in ET.parse(path).getroot().iter("testcase")]
                counts = report.xml_cases(path)
                row.update(attempt=asdict(attempt), xml=report.file_record(path), names=names, counts=counts)
            case _:
                raise ValueError("Apache consumer XML owner set differs")
        if required:
            allowed = required | {name.split("/", 1)[0] for name in required}
            if not required <= set(names) or set(names) - allowed or len(names) != len(set(names)):
                raise ValueError("Apache consumer omitted or added a selected case: " + str(names))
            if counts["passed"] < len(required) or counts["failed"] or counts["skipped"]:
                raise ValueError("Apache consumer failed or skipped a required case")
        else:
            # Go benchmarks do not necessarily produce named XML test cases.
            logs = [path for (_, name), path in retained.items() if name == "test.log"]
            match logs:
                case [path]:
                    row["benchmark_log"] = report.file_record(path)
                case _:
                    raise ValueError("Expected the benchmark's one retained native log")
            row.update(benchmarks=tested["executed"][0].get("benchmarks", []),
                       interpretation="Serving/caller integration only; no comparative performance claim")
        if tested["status"] != "PASS" or counts["failed"] or counts["skipped"]:
            raise RuntimeError("Existing Apache consumer failed: " + phase)
        row["status"] = "PASS"
        return row

    def diagnostics(runtime: str, config_sha: str, package: str, directory: Path) -> None:
        image = "sha256:" + config_sha
        def httpd(argument: str) -> str:
            name = runtime + "-diagnostic-" + uuid.uuid4().hex
            try:
                return command(["docker", "run", "--rm", "--name=" + name, "--runtime=" + runtime,
                                "--entrypoint=/usr/local/apache2/bin/httpd", image, argument])
            finally:
                result = subprocess.run(["docker", "rm", "--force", name], capture_output=True, text=True, timeout=30)
                records["commands"].append({"arguments": result.args, "native_exit": result.returncode,
                                             "stdout": result.stdout, "stderr": result.stderr})
                if result.returncode and "No such container" not in result.stderr:
                    raise RuntimeError("Cannot clean up the Apache executable diagnostic container")
        version = httpd("-v")
        if "Apache/2.4.69" not in version:
            raise ValueError("Runtime HTTPD version differs from the source producer")
        modules = httpd("-l")
        required_modules = {"event.c", "mod_deflate.c", "mod_filter.c", "mod_logio.c", "mod_reqtimeout.c", "mod_mime.c"}
        if not required_modules <= {line.strip() for line in modules.splitlines()}:
            raise ValueError("Runtime HTTPD omitted a required static module")
        httpd("-t")
        if package != "benchmarks":
            return
        name = runtime + "-response-" + uuid.uuid4().hex
        response_record = {"status": "INCOMPLETE", "container": name, "responses": []}
        records["response_contract"] = response_record
        try:
            # This is the unmodified command from the benchmark's runHttpd.
            command(["docker", "run", "--detach", "--name=" + name, "--runtime=" + runtime,
                     image, "sh", "-c", "mkdir -p /tmp/html; cp -r /local/* /tmp/html/.; apache2 -X"])
            inspected = json.loads(command(["docker", "inspect", name]))
            match inspected:
                case [{"NetworkSettings": {"IPAddress": address}}] if address:
                    pass
                case _:
                    raise ValueError("Apache diagnostic container has no IPv4 address")
            ready_deadline = time.monotonic() + min(30, remaining())
            while True:
                connection = http.client.HTTPConnection(address, 80, timeout=min(5, remaining()))
                try:
                    connection.request("GET", "/latin1k.txt", headers={"Accept-Encoding": "identity"})
                    response = connection.getresponse()
                    body = response.read(2048)
                    if response.status == 200 and body == bytes(1024):
                        break
                except (OSError, http.client.HTTPException):
                    if time.monotonic() >= ready_deadline:
                        raise
                finally:
                    connection.close()
                if time.monotonic() >= ready_deadline:
                    raise TimeoutError("Apache did not serve the copied benchmark payload within 30 seconds")
                time.sleep(0.1)
            for path, status, content_type in (("latin1k.txt", 200, "text/plain"), ("notfound", 404, "text/html")):
                plain = None
                for encoding in ("identity", "gzip"):
                    connection = http.client.HTTPConnection(address, 80, timeout=min(10, remaining()))
                    try:
                        connection.request("GET", "/" + path, headers={"Accept-Encoding": encoding})
                        response = connection.getresponse()
                        raw = response.read(1024 * 1024 + 1)
                        headers = {key.lower(): value for key, value in response.getheaders()}
                        if len(raw) > 1024 * 1024 or response.status != status or headers.get("content-type", "").split(";", 1)[0] != content_type:
                            raise ValueError("Apache response status/MIME/size differs: " + path)
                        if encoding == "gzip":
                            if headers.get("content-encoding") != "gzip":
                                raise ValueError("Apache omitted the configured gzip response filter")
                            body = gzip.decompress(raw)
                            if body != plain:
                                raise ValueError("Compression changed the Apache response bytes")
                        else:
                            if headers.get("content-encoding") not in (None, "identity"):
                                raise ValueError("Apache compressed an identity-only request")
                            body = plain = raw
                        if path == "latin1k.txt" and body != bytes(1024):
                            raise ValueError("The actual benchmark command did not preserve zero-filled content")
                        response_record["responses"].append({"path": path, "accept_encoding": encoding, "status": response.status,
                            "headers": headers, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                            "decoded_bytes": len(body), "decoded_sha256": hashlib.sha256(body).hexdigest()})
                    finally:
                        connection.close()
            response_record["status"] = "PASS"
        finally:
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True, timeout=30)
            (directory / "httpd-response-container.log").write_text(logs.stdout + logs.stderr)
            response_record["logs"] = report.file_record(directory / "httpd-response-container.log")
            cleanup = subprocess.run(["docker", "rm", "--force", name], capture_output=True, text=True, timeout=30)
            response_record["cleanup"] = {"native_exit": cleanup.returncode, "stdout": cleanup.stdout, "stderr": cleanup.stderr}
            if cleanup.returncode and "No such container" not in cleanup.stderr:
                raise RuntimeError("Failed to remove the task-owned Apache diagnostic container")

    try:
        records["containers_before"] = sorted(command(["docker", "ps", "--all", "--quiet", "--no-trunc"]).splitlines())
        for package in ("basic", "benchmarks"):
            contents_path = evidence / (package + "-httpd-contents.json")
            contents = report.read_json(contents_path)
            amd64 = next(row for row in contents["architectures"] if row["architecture"] == "amd64")
            runtime = "qualification-httpd-" + package
            directory = evidence / ("httpd-" + package)
            directory.mkdir()
            configuration = {"runtime_name": runtime, "image_name": package + "/httpd",
                             "runtime_flags": ["--platform=systrap", "--network=sandbox"], "contents": str(contents_path)}
            with prepared_runtime(directory / "runtime-preparation.json", private / runtime, configuration):
                diagnostics(runtime, amd64["config_sha256"], package, directory)
                if package == "basic":
                    consumer("httpd-image", "//test/image:image_test", runtime, "^TestHttpd$", {"TestHttpd"})
                    prepared = invoke("httpd-containerd-prepare", [*python, str(scripts / "root_image_runtime.py"), str(evidence), "prepare"], "command", targets=[])
                    if prepared["status"] != "PASS":
                        raise RuntimeError("Existing containerd harness preparation failed")
                    try:
                        required = {"TestCrictlSanity/" + group for group in ("enableGrouping", "disableGrouping")}
                        required |= {"TestMountPaths/" + group for group in ("enableGrouping", "disableGrouping")}
                        required.add("TestCrictlOneShim")
                        consumer("httpd-containerd", "//test/root:crictl_test", runtime,
                                 "^(TestCrictlSanity|TestMountPaths|TestCrictlOneShim)$", required,
                                 ["--test_arg=-harness_test_images=basic/httpd", "--test_arg=-containerd_version=2.2.3"])
                    finally:
                        checked = invoke("httpd-containerd-cleanup", [*python, str(scripts / "root_image_runtime.py"), str(evidence), "check"], "command", targets=[])
                        if checked["status"] != "PASS":
                            raise RuntimeError("Containerd consumer cleanup failed")
                else:
                    # These are the existing caller's native client/readiness
                    # fixtures, not part of the migrated Apache image closure.
                    command(["make", "--no-print-directory", "ARCH=x86_64", "DOCKER_BUILD=false",
                             "REMOTE_IMAGE_PREFIX=qualification.invalid/images", "rebuild-benchmarks_hey", "rebuild-benchmarks_util"])
                    records["benchmark_client_images"] = [json.loads(command(["docker", "image", "inspect", "gvisor.dev/images/benchmarks/" + name + ":latest"]))
                                                           for name in ("hey", "util")]
                    consumer("httpd-benchmark", "//test/benchmarks/network:httpd_test", runtime, "^$", set(),
                             ["--test_arg=-test.bench=^BenchmarkHttpd$/^filesize\\.(1Kb|1Mb|notfound)$/^concurrency\\.(1|25)$",
                              "--test_arg=-test.benchtime=100x", "--test_arg=-test.count=1"])
        records["containers_after"] = sorted(command(["docker", "ps", "--all", "--quiet", "--no-trunc"]).splitlines())
        if records["containers_after"] != records["containers_before"]:
            raise RuntimeError("Apache consumers left containers behind")
        records["status"] = "PASS"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, http.client.HTTPException) as error:
        records.update(status="FAIL", error=str(error))
        raise
    finally:
        report.write_json(evidence / "httpd-runtime-results.json", records)
