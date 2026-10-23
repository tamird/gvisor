"""Compare kernel checkpoint codecs using source-matched runtime builds."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request
from urllib.parse import unquote, urlsplit
import uuid

import qualification_report as report
from image_runtime import prepared_runtime


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    source = config["source"]
    workspace = Path.cwd()
    package = workspace / "test/rbe/state_array_pair"
    options = ["--config=x86_64", "--compilation_mode=opt", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc"]
    benchmark = "//test/benchmarks/checkpointfile:checkpointfile_test"
    build_targets = ["//:release", benchmark]
    record = {"source": source, "before_source": config["before_source"],
              "status": "INCOMPLETE", "builds": [], "cleanup": "INCOMPLETE"}
    inputs = {"harnesses": {}, "runtimes": {}, "benchstat": config["benchstat"]}
    runtime_inputs = {}
    tag = "gvisor.dev/images/benchmarks/nginx:latest"
    prior_image = None
    tag_installed = False
    cleanup_started = False

    def remaining() -> float:
        value = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic() + (90 if cleanup_started else -90)
        if value <= 0:
            raise TimeoutError("Kernel benchmark work deadline exhausted")
        return value

    def command(argv: list[str]) -> str:
        return subprocess.check_output(argv, text=True, timeout=min(60, remaining())).strip()

    def check_source(revision: str) -> None:
        if command(["git", "rev-parse", "HEAD"]) != revision:
            raise ValueError("Unexpected source revision")
        if command(["git", "status", "--porcelain", "--untracked-files=no"]):
            raise ValueError("Tracked source changed")
        for name, expected in config["matched_inputs"].items():
            path = workspace / name
            data = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("Common source input changed: " + name)

    def materialized(item: dict) -> Path:
        uri = urlsplit(item["uri"])
        if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
            raise ValueError("Build output was not materialized locally")
        path = Path(unquote(uri.path)).resolve(strict=True)
        path.relative_to(output_root.resolve())
        if report.file_record(path) != {key: item[key] for key in ("bytes", "sha256")}:
            raise ValueError("Materialized output differs from its configured BEP")
        if not os.access(path, os.X_OK):
            raise ValueError("Build output is not executable: " + str(path))
        return path

    try:
        check_source(source)
        for side, revision in (("after", source), ("before", config["before_source"])):
            command(["git", "switch", "--detach", revision])
            check_source(revision)
            directory = evidence / ("invocation-after-build" if side == "after" else "source-builds/before")
            result = invoke(side + "-build", ["build", *options, *build_targets], "build",
                            targets=build_targets, source=revision, directory=directory)
            if result["status"] != "PASS" or result["native_exit"] != 0:
                raise RuntimeError(side + " source-matched build failed")
            owners = {report.Owner(row["label"], row["configuration"]) for row in result["expected"]}
            if len(owners) != 2 or {owner.label for owner in owners} != set(build_targets):
                raise ValueError("Unexpected configured build owners")
            outputs = report.advertised_build_outputs(directory / "events.jsonl", {(owner, "default") for owner in owners})
            match [item for item in outputs if item["identity"]["label"] == benchmark]:
                case [harness]:
                    pass
                case _:
                    raise ValueError("Expected one source-matched benchmark executable")
            binary = materialized(harness)
            arm = package / "arms" / side
            arm.mkdir(parents=True)
            staged_binary = arm / "benchmark"
            shutil.copy2(binary, staged_binary)
            runfiles = Path(str(binary) + ".runfiles")
            if not runfiles.is_dir():
                raise ValueError("Benchmark build omitted runfiles")
            staged_runfiles = arm / "benchmark.runfiles"
            shutil.copytree(runfiles, staged_runfiles)
            files = {str(path.relative_to(package)): report.file_record(path)
                     for path in arm.rglob("*") if path.is_file()}
            if report.file_record(staged_binary) != {key: harness[key] for key in ("bytes", "sha256")}:
                raise ValueError("Copied benchmark differs from its configured output")
            inputs["harnesses"][side] = {
                "source": revision, "owner": harness["identity"],
                "binary": str(staged_binary.relative_to(package)),
                "runfiles_root": str(staged_runfiles.relative_to(package)), "files": files,
                "benchmark_source_sha256": hashlib.sha256(
                    (workspace / "test/benchmarks/checkpointfile/checkpointfile.go").read_bytes()).hexdigest(),
            }
            release = [item for item in outputs if item["identity"]["label"] == "//:release"]
            by_name = {item["name"].split("/release/", 1)[1]: item for item in release}
            if len(release) != 8 or set(by_name) != set(config["runtime_paths"]) | {"containerd-shim-runsc-v1"}:
                raise ValueError("Release build has an unexpected output set")
            runtime_directory = private / (side + "-runtime")
            retained = {}
            for name in config["runtime_paths"]:
                item = by_name[name]
                original = materialized(item)
                destination = runtime_directory / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, destination)
                expected = {key: item[key] for key in ("bytes", "sha256")}
                if report.file_record(destination) != expected:
                    raise ValueError("Copied runtime differs from its configured output")
                retained[name] = expected
            runtime_inputs[side] = (revision, runtime_directory, retained)
            report.write_json(directory / "copied-outputs.json", outputs)
            record["builds"].append({"side": side, "source": revision,
                                    "verdict": str((directory / "verdict.json").relative_to(evidence)),
                                    "outputs": outputs})
        command(["git", "switch", "--detach", source])
        check_source(source)

        selected = [*options, "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                    "--nozip_undeclared_test_outputs", "--test_sharding_strategy=disabled",
                    "--test_env=GO_TEST_WRAP_TESTV=1"]
        correctness = config["correctness_targets"]
        for name, extra in (("normal", []), ("race", ["--config=race"])):
            result = invoke("correctness-" + name, ["test", *selected, *extra, *correctness],
                            "test", targets=correctness)
            if result["status"] != "PASS" or result["case_counts"].get("skipped", 0):
                raise RuntimeError("State correctness checks failed or skipped: " + name)
        # Each Nogo owner checks both architectures through its split transition;
        # its filter executable must still run on this AMD64 coordinator.
        result = invoke("analyzers", ["test", *selected, *config["analyzer_targets"]],
                        "test", targets=config["analyzer_targets"])
        if result["status"] != "PASS":
            raise RuntimeError("State analyzer checks failed")

        benchstat = config["benchstat"]
        request = urllib.request.Request(
            "https://app.buildbuddy.io/api/v1/GetFile", data=json.dumps({"uri": benchstat["uri"]}).encode(),
            headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        with urllib.request.urlopen(request, timeout=min(60, remaining())) as response:
            data = response.read(benchstat["bytes"] + 1)
        if len(data) != benchstat["bytes"] or hashlib.sha256(data).hexdigest() != benchstat["sha256"]:
            raise ValueError("Retained source-built benchstat identity mismatch")
        (package / "benchstat").write_bytes(data)
        (package / "benchstat").chmod(0o755)

        fixture = config["fixture"]
        command(["docker", "pull", fixture["uri"]])
        image = json.loads(command(["docker", "image", "inspect", fixture["uri"]]))[0]
        if image["Id"] != fixture["id"] or image["Architecture"] != "amd64":
            raise ValueError("Pulled benchmark fixture differs from the pinned registry image")
        probe = "state-kernel-probe-" + uuid.uuid4().hex
        try:
            command(["docker", "create", "--name=" + probe, "--runtime=runc", "--entrypoint=cat",
                     fixture["uri"], "/etc/nginx/nginx_gofer.conf"])
            payload = subprocess.check_output(
                ["docker", "start", "--attach", probe], timeout=min(60, remaining()))
        finally:
            subprocess.run(["docker", "rm", "--force", probe], check=True, timeout=60)
        if hashlib.sha256(payload).hexdigest() != fixture["gofer_config_sha256"]:
            raise ValueError("Benchmark image contains different nginx settings")
        existing = command(["docker", "image", "ls", "--format={{.Repository}}:{{.Tag}}"])
        if tag in existing.splitlines():
            prior_image = command(["docker", "image", "inspect", tag, "--format={{.Id}}"])
        command(["docker", "tag", image["Id"], tag])
        tag_installed = True
        inputs["image"] = {"id": image["Id"], "tag": tag, "fixture": fixture}
        with ExitStack() as scopes:
            for side, (revision, runtime_directory, retained) in runtime_inputs.items():
                directory = evidence / (side + "-runtime")
                directory.mkdir()
                name = "runsc-state-kernel-" + side
                runtime = scopes.enter_context(prepared_runtime(directory / "runtime.json", runtime_directory, {
                    "runtime_name": name, "runtime_flags": ["--platform=systrap"],
                    "image_name": "benchmarks/nginx", "lifecycle_only": True, "cleanup_reserve_seconds": 90,
                    "retained_release_files": retained, "release_supplier": {"source": revision},
                }))
                inputs["runtimes"][side] = {"name": name, "source": revision, "files": runtime["runtime_files"],
                                           "configuration": runtime["config_after"]["runtimes"][name]}
            report.write_json(package / "inputs.json", inputs)
            report.write_json(evidence / "kernel-benchmark-inputs.json", inputs)
            target = ["//test/rbe/state_array_pair:paired_test"]
            analysis = invoke("paired-analysis", ["test", *selected, "--nobuild", *target], "analysis", targets=target)
            if analysis["status"] != "PASS":
                raise RuntimeError("Paired checkpoint benchmark analysis failed")
            result = invoke("paired", ["test", *selected, *target], "test",
                            (evidence / "invocation-paired-analysis/events.jsonl",), targets=target)
            if result["status"] != "PASS" or result["native_exit"] != 0:
                raise RuntimeError("Paired checkpoint benchmark failed")
        record["status"] = "PASS"
    finally:
        cleanup_started = True
        completed_status = "PASS" if record["status"] == "PASS" else "FAIL"
        record["status"] = "FAIL"
        try:
            try:
                command(["git", "switch", "--detach", source])
                check_source(source)
            finally:
                if tag_installed:
                    command(["docker", "tag", prior_image, tag] if prior_image else ["docker", "image", "rm", tag])
            record["cleanup"] = "PASS"
            record["status"] = completed_status
        finally:
            report.write_json(evidence / "kernel-benchmark-builds.json", record)
