"""Checkpoint one warm instance of the existing Abseil/Bazel workload."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit
import urllib.request

import qualification_report as report
from docker_checkpoint import materialize_carrier
from image_runtime import prepared_runtime


def check_input_files(config: dict) -> None:
    for name, expected in config["matched_inputs"].items():
        path = Path(name)
        data = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Qualification source input changed: " + name)


def decode_profile(invoke, evidence: Path, profile: Path, output: Path) -> dict:
    """Decode embedded symbols with the repository's declared pprof dependency."""
    output.mkdir(exist_ok=True)
    target = "@com_github_google_pprof//:pprof"
    options = ["--config=x86_64", "--compilation_mode=opt", "--nostamp", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc"]
    built = invoke("profile-reader-build", ["build", *options, target], "build", targets=[target])
    if built["status"] != "PASS" or built["native_exit"] != 0:
        raise RuntimeError("Declared pprof build failed")
    owners = {report.Owner(row["label"], row["configuration"]) for row in built["expected"]}
    if len(owners) != 1:
        raise ValueError("Expected one configured pprof owner")
    reader, = report.advertised_build_outputs(evidence / "invocation-profile-reader-build" / "events.jsonl",
                                             {(owner, "default") for owner in owners})
    uri = urlsplit(reader["uri"])
    if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
        raise ValueError("Profile reader output was not materialized")
    executable = Path(unquote(uri.path)).resolve(strict=True)
    executable.relative_to(Path(os.environ["APPLICABILITY_OUTPUT_BASE"]).resolve())
    if (report.file_record(executable) != {key: reader[key] for key in ("bytes", "sha256")} or
            not os.access(executable, os.X_OK)):
        raise ValueError("Materialized pprof executable differs from its configured output")
    reports = {}
    for name, arguments in (
        ("raw", ["-raw"]),
        ("top", ["-top", "-nodecount=80"]),
        ("state-traces", ["-traces", "-focus=gvisor.dev/gvisor/pkg/state.*Load"]),
    ):
        phase = "profile-decode-" + name
        result = invoke(phase, [
            "run", *options,
            "--run_under=timeout --signal=TERM --kill-after=5s 60s",
            target, "--", "-symbolize=none",
            *arguments, str(profile),
        ], "command", targets=[])
        if result["status"] != "PASS" or result["native_exit"] != 0:
            raise RuntimeError("Declared pprof report failed: " + name)
        destination = output / ("restore-cpu." + name + ".txt")
        shutil.copyfile(evidence / ("invocation-" + phase) / "stdout.log", destination)
        reports[destination.name] = report.file_record(destination)
    raw = (output / "restore-cpu.raw.txt").read_text()
    traces = (output / "restore-cpu.state-traces.txt").read_text()
    samples = raw.split("Samples:", 1)[1].split("Locations", 1)[0]
    values = [tuple(map(int, match)) for match in re.findall(r"^\s+(\d+)\s+(\d+):", samples, re.MULTILINE)]
    if ("samples/count cpu/nanoseconds" not in " ".join(samples.split()) or not values or
            sum(value[0] for value in values) <= 0 or sum(value[1] for value in values) <= 0 or
            "gvisor.dev/gvisor/pkg/state.Load" not in traces):
        raise ValueError("Restored profile has no native CPU samples attributed to state.Load")
    return {"sample_count": sum(value[0] for value in values),
            "cpu_nanoseconds": sum(value[1] for value in values), "reports": reports, "reader": reader}


def report_retained_profile(invoke, config: dict, evidence: Path, private: Path) -> None:
    """Complete only native reports; retain the original workload's failure."""
    supplier = config["profile_supplier"]
    record = {"status": "FAIL", "supplier": supplier, "source": config["source"]}
    try:
        artifact = supplier["artifact"]
        request = urllib.request.Request(
            "https://app.buildbuddy.io/api/v1/GetFile", data=json.dumps({"uri": artifact["uri"]}).encode(),
            headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(artifact["bytes"] + 1)
        if {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} != {key: artifact[key] for key in ("bytes", "sha256")}:
            raise ValueError("Retained profile bundle identity changed")
        archive = private / "profile-supplier.tar.gz"
        archive.write_bytes(data)
        members = report.bundle_members(archive, set(supplier["members"]), verify=supplier["members"],
                                        deadline=float(os.environ["APPLICABILITY_WORK_DEADLINE"]))
        aggregate = json.loads(members["verdict.json"])
        phase = json.loads(members["invocation-warm-checkpoint/verdict.json"])
        if (aggregate["status"] != "FAIL" or aggregate["sources"] != [supplier["source"]] or
                phase["source"] != supplier["source"] or phase["status"] != "PASS" or phase["native_exit"] != 0 or
                phase["case_counts"] != {"passed": 1, "failed": 0, "skipped": 0}):
            raise ValueError("Retained workload native status or source differs")
        output = evidence / "warm-runtime"
        output.mkdir()
        profile = output / "restore-cpu.pprof"
        profile.write_bytes(members[supplier["profile"]])
        record["profile"] = {"path": str(profile.relative_to(evidence)), **report.file_record(profile)}
        record["decoded"] = decode_profile(invoke, evidence, profile, output)
        record["status"] = "PASS"
    finally:
        report.write_json(evidence / "warm-absl-profile-reports.json", record)


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    if config["workload"] == "warm_absl_profile_reports":
        if config["targets"] != [] or config["image_targets"] != {"x86_64": ["@com_github_google_pprof//:pprof"]}:
            raise ValueError("Profile reporting must prepare only the declared pprof reader")
        check_input_files(config)
        report_retained_profile(invoke, config, evidence, private)
        return
    source = config["source"]
    workspace = Path.cwd()
    target = "//test/benchmarks/fs:bazel_test"
    if config["targets"] != [target] or config["workload"] != "warm_absl":
        raise ValueError("Expected the single warm Abseil checkpoint owner")
    profile_cpu = config.get("profile_cpu", False)
    if type(profile_cpu) is not bool:
        raise ValueError("profile_cpu must be a boolean")
    options = ["--config=x86_64", "--compilation_mode=opt", "--nostamp", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--build_event_json_file_path_conversion=false",
               "--noannounce_rc"]
    record = {"source": source, "status": "INCOMPLETE", "cleanup": "INCOMPLETE"}
    tag = "gvisor.dev/images/benchmarks/absl:latest"
    prior_image = None
    tag_installed = False
    cleanup_started = False
    initial_containers = None

    def remaining() -> float:
        value = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic() - (0 if cleanup_started else 90)
        if value <= 0:
            raise TimeoutError("Warm checkpoint work deadline exhausted")
        return value

    def command(argv: list[str], limit: float = 60) -> str:
        return subprocess.check_output(argv, text=True, timeout=min(limit, remaining())).strip()

    def check_source() -> None:
        if command(["git", "rev-parse", "HEAD"]) != source:
            raise ValueError("Unexpected source revision")
        if command(["git", "status", "--porcelain", "--untracked-files=no"]):
            raise ValueError("Tracked source changed")
        check_input_files(config)

    def materialized(item: dict) -> Path:
        uri = urlsplit(item["uri"])
        if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
            raise ValueError("Build output was not materialized locally")
        path = Path(unquote(uri.path)).resolve(strict=True)
        path.relative_to(output_root.resolve())
        if report.file_record(path) != {key: item[key] for key in ("bytes", "sha256")} or not os.access(path, os.X_OK):
            raise ValueError("Materialized executable differs from its configured BEP")
        return path

    try:
        check_source()
        formatter = shlex.join([
            "bash", "-c", 'set -eu; export PATH="$(dirname "$1"):$PATH"; '
            'cd "$BUILD_WORKSPACE_DIRECTORY"; exec bash tools/lint.sh gofmt', "--",
        ])
        formatted = invoke("gofmt", ["run", *options, "--run_under=" + formatter,
                                     "@io_bazel_rules_go//go:go"], "command", targets=[])
        if formatted["status"] != "PASS":
            raise RuntimeError("Canonical Go formatting failed")
        build_targets = ["//:release", target]
        directory = evidence / "invocation-runtime-build"
        result = invoke("runtime-build", ["build", *options, *build_targets], "build", targets=build_targets)
        if result["status"] != "PASS" or result["native_exit"] != 0:
            raise RuntimeError("Source-matched runtime and test build failed")
        owners = {report.Owner(row["label"], row["configuration"]) for row in result["expected"]}
        if len(owners) != 2 or {owner.label for owner in owners} != set(build_targets):
            raise ValueError("Unexpected configured build owners")
        outputs = report.advertised_build_outputs(directory / "events.jsonl", {(owner, "default") for owner in owners})
        release = [item for item in outputs if item["identity"]["label"] == "//:release"]
        actual = {item["name"]: {key: item[key] for key in ("bytes", "sha256")} for item in release}
        if len(release) != 8 or actual != config["qualified_release_outputs"]:
            raise ValueError("Runtime differs from the qualified typed lifecycle release")
        match [item for item in outputs if item["identity"]["label"] == target]:
            case [test_binary]:
                materialized(test_binary)
            case _:
                raise ValueError("Expected exactly one warm checkpoint test executable")
        runtime_directory = private / "runtime"
        by_name = {item["name"].split("/release/", 1)[1]: item for item in release}
        if set(by_name) != set(config["runtime_paths"]) | {"containerd-shim-runsc-v1"}:
            raise ValueError("Unexpected release output names")
        retained = {}
        for name in config["runtime_paths"]:
            item = by_name[name]
            destination = runtime_directory / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(materialized(item), destination)
            retained[name] = {key: item[key] for key in ("bytes", "sha256")}
            if report.file_record(destination) != retained[name]:
                raise ValueError("Copied runtime differs from configured output")
        record["outputs"] = outputs
        record["release_files"] = retained
        carrier = materialize_carrier(config, private, evidence, time.monotonic() + remaining())
        fixture = config["fixture"]
        command(["docker", "pull", fixture["uri"]], limit=300)
        image, = json.loads(command(["docker", "image", "inspect", fixture["uri"]]))
        if image["Id"] != fixture["id"] or image["Architecture"] != "amd64":
            raise ValueError("Pulled fixture differs from the immutable published Abseil image")
        record["fixture"] = {"kind": "pulled immutable existing fixture", "expected": fixture, "inspect": image}
        existing = command(["docker", "image", "ls", "--format={{.Repository}}:{{.Tag}}"])
        if tag in existing.splitlines():
            prior_image = command(["docker", "image", "inspect", tag, "--format={{.Id}}"])
        command(["docker", "tag", image["Id"], tag])
        tag_installed = True
        initial_containers = command(["docker", "ps", "--all", "--quiet", "--no-trunc"]).splitlines()
        if initial_containers:
            raise ValueError("Disposable Docker coordinator already has containers")
        runtime_name = "runsc-warm-absl"
        runtime_evidence = evidence / "warm-runtime"
        runtime_evidence.mkdir()
        runtime_flags = ["--platform=systrap", "--debug=true", "--debug-command=restore,checkpoint,boot"]
        if profile_cpu:
            runtime_flags.extend(["--profile=true", "--profile-cpu=" + str(runtime_evidence / "cpu.%TIMESTAMP%.%CID%.pprof")])
        with prepared_runtime(runtime_evidence / "runtime.json", runtime_directory, {
            "runtime_name": runtime_name,
            "runtime_flags": runtime_flags,
            "image_name": "benchmarks/absl", "lifecycle_only": True, "cleanup_reserve_seconds": 90,
            "docker_experimental": True, "docker_version": "24.0.9", "docker_binary": carrier["dockerd"],
            "retained_release_files": retained, "release_supplier": {"source": source},
        }) as runtime:
            selected = [*options, "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                        "--nozip_undeclared_test_outputs", "--test_sharding_strategy=disabled", "--test_timeout=1200",
                        "--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=1",
                        "--test_arg=-test.run=^TestCheckpointWarmABSL$", "--test_arg=-test.bench=^$",
                        "--test_arg=-runtime=" + runtime_name]
            result = invoke("warm-checkpoint", ["test", *selected, target], "test", targets=[target], retain_outputs=True)
            if (result["status"] != "PASS" or result["native_exit"] != 0 or
                    result["case_counts"] != {"passed": 1, "failed": 0, "skipped": 0}):
                raise RuntimeError("Warm checkpoint did not complete the single positive test")
            match result["executed"]:
                case [attempt]:
                    if attempt["cached_locally"] or attempt["execution_info"].get("cachedRemotely", False):
                        raise ValueError("Warm checkpoint was cached")
                case _:
                    raise ValueError("Expected exactly one warm checkpoint attempt")
            pid = int(Path("/var/run/docker.pid").read_text())
            process = Path("/proc") / str(pid)
            observed = {"pid": pid, "start_ticks": (process / "stat").read_text().rsplit(")", 1)[1].split()[19],
                        "executable": {"path": str((process / "exe").resolve()), **report.file_record(process / "exe")},
                        "server": json.loads(command(["docker", "version", "--format={{json .Server}}"]))}
            if observed != runtime["fixture_daemon"]:
                raise ValueError("Checkpoint used a changed Docker daemon")
            record["daemon_after_test"] = observed
        if profile_cpu:
            # Sentry profile FDs use the same start timestamp/CID as boot logs.
            # Gofer profiles have a separate suffix. The two sentry processes
            # must finish and flush before inspecting their native output.
            profiles = []
            for log in sorted((runtime_evidence / "runsc-logs").glob("*.boot.txt")):
                text = log.read_text()
                cid, = re.findall(r"Args: .* ([a-f0-9]{64})\]$", text, re.M)
                stamp = log.name.removeprefix("runsc.log.").removesuffix(".boot.txt")
                path = runtime_evidence / f"cpu.{stamp}.{cid}.pprof"
                if not path.is_file() or path.stat().st_size == 0:
                    raise ValueError("Missing nonempty sentry profile: " + str(path))
                if "CPU profiling enabled" not in text or "CPU profiling stopped" not in text:
                    raise ValueError("Sentry profile lifecycle did not complete")
                profiles.append({"role": "restore" if "Kernel load took [" in text else "initial",
                                 "container": cid, "log": str(log.relative_to(evidence)),
                                 "path": str(path.relative_to(evidence)), **report.file_record(path)})
            if len(profiles) != 2 or {row["role"] for row in profiles} != {"initial", "restore"} or len({row["container"] for row in profiles}) != 1:
                raise ValueError("Expected exactly the initial and restored sentry profiles")
            restored, = [row for row in profiles if row["role"] == "restore"]
            profile = evidence / restored["path"]
            decoded = decode_profile(invoke, evidence, profile, runtime_evidence)
            restored.update({key: decoded[key] for key in ("sample_count", "cpu_nanoseconds")})
            record["profiles"] = profiles
            record["profile_reports"] = decoded["reports"]
            record["profile_reader"] = decoded["reader"]
        record["status"] = "PASS"
    finally:
        cleanup_started = True
        completed_status = record["status"]
        record["status"] = "FAIL"
        errors = []
        try:
            check_source()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(str(error))
        if tag_installed:
            try:
                command(["docker", "tag", prior_image, tag] if prior_image else ["docker", "image", "rm", tag])
            except (OSError, subprocess.SubprocessError) as error:
                errors.append(str(error))
        if initial_containers is not None:
            try:
                record["final_containers"] = command(["docker", "ps", "--all", "--quiet", "--no-trunc"]).splitlines()
                if record["final_containers"] != initial_containers:
                    errors.append("Residual containers; retained without deleting unowned state")
            except (OSError, subprocess.SubprocessError) as error:
                errors.append(str(error))
        record["cleanup_errors"] = errors
        record["cleanup"] = "FAIL" if errors else "PASS"
        if not errors:
            record["status"] = completed_status
        report.write_json(evidence / "warm-absl-workload.json", record)
        if errors:
            raise RuntimeError("Warm checkpoint cleanup failed: " + "; ".join(errors))
