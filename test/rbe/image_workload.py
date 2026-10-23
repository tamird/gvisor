"""Measure a real HTTP response after restoring the two qualified runtimes."""
from contextlib import ExitStack
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import time

import qualification_report as report
from image_runtime import prepared_runtime
from retained_release import materialize_runtime


def restore_stages(log_dir: Path) -> list[dict]:
    """Keep timestamps and event types; exclude hook environments and arguments."""
    stages = []
    for path in sorted(log_dir.glob("*.restore.txt")):
        year = int(path.name.split("runsc.log.")[1][:4])
        events = []
        pending = None
        for line in path.read_text().splitlines():
            match = re.match(r"^[IDW](\d{4} \d\d:\d\d:\d\d\.\d{6})\s+\d+ ([^ :]+):\d+\] (.*)$", line)
            if match is None:
                continue
            timestamp, source, message = match.groups()
            if message.startswith("Executing hook "):
                if pending is not None:
                    raise ValueError("Overlapping OCI hooks in one CLI process")
                args = message.partition(" Args:[")[2].partition("]")[0].split()
                pending = "libnetwork-setkey" if "libnetwork-setkey" in args[:2] else "other"
                name = "hook-start:" + pending
            elif message.startswith("Execute hook ") and message.endswith(" success!"):
                if pending is None:
                    raise ValueError("Hook finished without a start")
                name, pending = "hook-end:" + pending, None
            elif message.startswith("Container not found, creating new one,"):
                name = "create-for-restore"
            elif message.startswith("FD parking sidecar started,"):
                name = "sidecar-started"
            elif message.startswith("Restore: "):
                name = "restore-command"
            elif message.startswith("Restore container, cid: "):
                name = "restore-entry" if source == "restore.go" else "container-restore"
            elif message == "Exiting with status: 0":
                name = "exit"
            else:
                continue
            when = datetime.datetime.strptime(str(year) + timestamp, "%Y%m%d %H:%M:%S.%f")
            events.append({"time": when.isoformat(), "event": name})
        if pending is not None or [e["event"] for e in events if not e["event"].startswith("hook-")] != [
                "restore-entry", "create-for-restore", "sidecar-started", "restore-command", "container-restore", "exit"]:
            raise ValueError("Incomplete or reordered restore CLI events")
        stages.append({"log": path.name, "events": events})
    if len(stages) != 2:
        raise ValueError("Expected two fresh restore CLI traces")
    return stages


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    [target] = config["targets"]
    sides = ("before", "after")
    tag = "gvisor.dev/images/benchmarks/nginx:latest"
    fixture = config["fixture"]
    record = {"source": config["source"], "status": "INCOMPLETE", "arms": {}}
    deadline = float(os.environ["APPLICABILITY_WORK_DEADLINE"])

    def docker(*args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Restore pilot work deadline expired")
        return subprocess.check_output(["docker", *args], text=True, timeout=min(120, remaining)).strip()

    def containers():
        return sorted(docker("ps", "-aq", "--no-trunc").split())

    options = ["--config=x86_64", "--compilation_mode=opt", "--lockfile_mode=error",
               "--extra_toolchains=@llvm//toolchain:all", "--remote_download_outputs=all", "--noannounce_rc"]
    expected = evidence / "restore-benchmarks.json"
    report.write_json(expected, {"owners": {target: [{"name": "BenchmarkRestoreNginx", "count": 2,
        "iterations": 1, "units": ["ns/op", "restore-ns/op"]}]}})
    original = containers()
    original_tag = None
    installed = False
    try:
        # Compile the common client before changing the Docker configuration.
        built = invoke("client-build", ["build", *options, target], "build", targets=[target])
        if built["status"] != "PASS":
            raise RuntimeError("Could not build the restore benchmark")
        [configured] = built["expected"]
        [client] = report.advertised_build_outputs(evidence / "invocation-client-build/events.jsonl",
            {(report.Owner(configured["label"], configured["configuration"]), "default")})
        record["client"] = client
        if tag in docker("image", "ls", "--format={{.Repository}}:{{.Tag}}").splitlines():
            original_tag = docker("image", "inspect", "--format={{.Id}}", tag)
        docker("pull", fixture["uri"])
        [image] = json.loads(docker("image", "inspect", fixture["uri"]))
        if (image["Id"], image["Os"], image["Architecture"]) != (fixture["id"], "linux", "amd64"):
            raise ValueError("Nginx fixture does not match the paired checkpoint run")
        docker("tag", image["Id"], tag)
        installed = True
        with ExitStack() as stack:
            for side in sides:
                arm = evidence / side
                arm.mkdir()
                supplier = config["suppliers"][side]
                runtime_dir = private / side
                files = materialize_runtime(supplier, runtime_dir, arm)
                name = "runsc-state-response-" + side
                prepared = stack.enter_context(prepared_runtime(arm / "runtime.json", runtime_dir, {
                    "runtime_name": name, "runtime_flags": ["--platform=systrap", "--network=sandbox",
                                                           "--debug", "--debug-command=restore"],
                    "image_name": "benchmarks/nginx", "lifecycle_only": True, "cleanup_reserve_seconds": 90,
                    "docker_experimental": True, "docker_version": "24.0.9",
                    "retained_release_files": files, "release_supplier": {"source": supplier["source"]},
                }))
                record["arms"][side] = {"source": supplier["source"], "runtime": name,
                                       "runtime_files": prepared["runtime_files"]}
            for side in sides:
                arm = record["arms"][side]
                args = [*options, "--nocache_test_results", "--flaky_test_attempts=1",
                        "--runs_per_test=1", "--test_sharding_strategy=disabled", "--test_timeout=180",
                        "--nozip_undeclared_test_outputs", "--test_env=GO_TEST_WRAP_TESTV=0",
                        "--test_arg=-test.v=false", "--test_arg=-test.run=^$", "--test_filter=^$",
                        "--test_arg=-test.bench=^BenchmarkRestoreNginx$", "--test_arg=-test.benchtime=1x",
                        "--test_arg=-test.count=2", "--test_arg=-test-checkpoint=true",
                        "--test_arg=-runtime=" + arm["runtime"]]
                if docker("image", "inspect", "--format={{.Id}}", tag) != fixture["id"]:
                    raise ValueError("Benchmark fixture changed between arms")
                analysis = invoke(side + "-analysis", ["test", *args, "--nobuild", target], "analysis", targets=[target])
                if analysis["status"] != "PASS":
                    raise RuntimeError(side + " benchmark analysis failed")
                tested = invoke(side + "-response", ["test", *args, target], "test",
                    (evidence / ("invocation-" + side + "-analysis/events.jsonl"),),
                    targets=[target], declared_benchmarks=expected)
                if tested["status"] != "PASS" or tested["native_exit"] != 0:
                    raise RuntimeError(side + " restore/first-response benchmark failed")
                [attempt] = tested["executed"]
                if attempt["cached_locally"] or attempt["execution_info"].get("cachedRemotely", False):
                    raise ValueError("Restore benchmark was not executed")
                [configured] = tested["expected"]
                [executed_client] = report.advertised_build_outputs(
                    evidence / ("invocation-" + side + "-response/events.jsonl"),
                    {(report.Owner(configured["label"], configured["configuration"]), "default")})
                if any(executed_client[k] != client[k] for k in ("name", "sha256", "bytes")):
                    raise ValueError("Executed restore client differs from common build")
                arm["benchmarks"] = attempt["benchmarks"]
                arm["restore_stages"] = restore_stages(private / side / "runsc-logs")
                if containers() != original:
                    raise RuntimeError("Restore benchmark left a container behind")
        if any(json.loads((evidence / side / "runtime.json").read_text())["cleanup"] != "PASS" for side in sides):
            raise RuntimeError("Runtime cleanup incomplete")
        record["status"] = "PASS"
    finally:
        try:
            if installed:
                if original_tag is None:
                    docker("image", "rm", tag)
                else:
                    docker("tag", original_tag, tag)
            if containers() != original:
                raise RuntimeError("Worker container inventory changed")
            record["cleanup"] = "PASS"
        finally:
            if record.get("cleanup") != "PASS":
                record["status"] = "FAIL"
            report.write_json(evidence / "state-restore-response.json", record)
