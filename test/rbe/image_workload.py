"""Checkpoint one warm instance of the existing Abseil/Bazel workload."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
from urllib.parse import unquote, urlsplit

import qualification_report as report
from docker_checkpoint import materialize_carrier
from image_runtime import prepared_runtime


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    source = config["source"]
    workspace = Path.cwd()
    target = "//test/benchmarks/fs:bazel_test"
    if config["targets"] != [target] or config["workload"] != "warm_absl":
        raise ValueError("Expected the single warm Abseil checkpoint owner")
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
        for name, expected in config["matched_inputs"].items():
            path = workspace / name
            data = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("Qualification source input changed: " + name)

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
        qualified = config["qualified_release_outputs"]
        if len(release) != 8 or set(actual) != set(qualified):
            raise ValueError("Release output census differs from the qualified control")
        if config.get("root_census", False):
            # The diagnostic changes the sentry. The materialization and install
            # checks below still bind every binary to this build's BEP digest.
            sentry = "bazel-out/k8-opt/bin/release/gvisor-bin/gvisor_sentry"
            if actual[sentry] == qualified[sentry]:
                raise ValueError("The control sentry was built instead of the instrumented one")
            for script in ("gvisor-proc-umounter", "gvisor-sentry-prewarmer", "runsc-fd-parking"):
                name = "bazel-out/k8-opt/bin/release/gvisor-bin/" + script
                if actual[name] != qualified[name]:
                    raise ValueError("Unchanged release helper differs from the control: " + name)
        elif actual != qualified:
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
        with prepared_runtime(runtime_evidence / "runtime.json", runtime_directory, {
            "runtime_name": runtime_name,
            "runtime_flags": ["--platform=systrap", "--debug=true", "--debug-command=restore,checkpoint,boot"],
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
        if config.get("root_census", False):
            restored = []
            for path in (runtime_evidence / "runsc-logs").glob("*.boot.txt"):
                text = path.read_text()
                if "Kernel load stats: " in text:
                    restored.append((path, text))
            (path, text), = restored
            _, table = text.split("Kernel load stats: ")
            table, _ = table.split("Kernel load took [", 1)
            counts = {}
            for line in table.splitlines():
                if not line.startswith("STATE_ROOT\t"):
                    continue
                _, site, count, size, package, typ = line.split("\t", 5)
                count, size = int(count), int(size)
                key = (site, package, typ)
                if key in counts or site not in ("interior", "standalone", "pointer") or count <= 0 or size < 0:
                    raise ValueError("Invalid root allocation row: " + line)
                counts[key] = {"site": site, "package": package, "type": typ, "count": count, "object_bytes": size}
            if not counts:
                raise ValueError("The restored kernel reported no typed root allocations")
            record["root_census"] = {"log": str(path.relative_to(evidence)), **report.file_record(path),
                                     "rows": [counts[key] for key in sorted(counts)]}
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
