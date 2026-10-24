"""Qualify platform filter use and compare two uninstrumented runtime builds."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request

import qualification_report as report
from image_runtime import prepared_runtime


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    source = config["source"]
    workspace = Path.cwd()
    package = workspace / "test/rbe/state_array_pair"
    options = ["--config=x86_64", "--compilation_mode=opt", "--lockfile_mode=error",
               "--extra_toolchains=@llvm//toolchain:all", "--remote_download_outputs=all",
               "--build_event_json_file_path_conversion=false", "--noannounce_rc"]
    records = {"status": "INCOMPLETE", "source": source, "product": config["product"],
               "before_source": config["before_source"], "runtime_builds": [], "platform_logs": {}}
    before_tag = None
    tag_installed = False
    tag = "gvisor.dev/images/benchmarks/alpine:latest"
    initial_containers = None

    def remaining() -> float:
        value = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic() - 90
        if value <= 0:
            raise TimeoutError("Startup qualification exhausted its work deadline")
        return value

    def command(argv: list[str]) -> str:
        return subprocess.check_output(argv, text=True, timeout=min(60, remaining())).strip()

    def check_source(revision: str) -> None:
        if command(["git", "rev-parse", "HEAD"]) != revision:
            raise ValueError("Unexpected runtime source")
        if command(["git", "status", "--porcelain", "--untracked-files=no"]):
            raise ValueError("Runtime source has tracked edits")
        for name, expected in config["matched_inputs"].items():
            if hashlib.sha256((workspace / name).read_bytes()).hexdigest() != expected:
                raise ValueError("Shared startup source changed: " + name)

    def fetch(record: dict, destination: Path) -> None:
        request = urllib.request.Request(
            "https://app.buildbuddy.io/api/v1/GetFile",
            data=json.dumps({"uri": record["uri"]}).encode(),
            headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        with urllib.request.urlopen(request, timeout=min(60, remaining())) as response:
            data = response.read(record["bytes"] + 1)
        if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError("Retained startup input identity mismatch: " + str(destination))
        destination.write_bytes(data)

    def materialized(item: dict) -> Path:
        relative = Path(item["name"])
        if relative.is_absolute() or relative.parts[:1] != ("bazel-out",) or ".." in relative.parts:
            raise ValueError("Unexpected configured output path")
        path = (workspace / relative).resolve(strict=True)
        path.relative_to(output_root.resolve())
        if report.file_record(path) != {key: item[key] for key in ("bytes", "sha256")}:
            raise ValueError("Materialized output differs from its configured BEP output")
        return path

    try:
        check_source(source)
        initial_containers = sorted(command(["docker", "ps", "-aq", "--no-trunc"]).split())
        records["host"] = {"uname": list(os.uname()), "cpuinfo": Path("/proc/cpuinfo").read_text(),
                           "affinity": sorted(os.sched_getaffinity(0)),
                           "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                           "initial_containers": initial_containers}
        benchstat = config["benchstat"]
        fetch(benchstat, package / "benchstat")
        (package / "benchstat").chmod(0o755)
        report.write_json(package / "inputs.json", {})
        fixture = config["fixture"]
        archive = private / "sleep-image.tar"
        fetch(fixture, archive)
        result = subprocess.run(["docker", "image", "inspect", tag, "--format={{.Id}}"],
                                capture_output=True, text=True, timeout=min(30, remaining()))
        if result.returncode == 0:
            before_tag = result.stdout.strip()
        else:
            command(["docker", "info", "--format={{.ID}}"])
            if tag in command(["docker", "image", "ls", "--format={{.Repository}}:{{.Tag}}"]).splitlines():
                raise RuntimeError("Existing image tag could not be inspected")
        command(["docker", "load", "--input", str(archive)])
        match json.loads(command(["docker", "image", "inspect", "sha256:" + fixture["config_sha256"]])):
            case [{"Architecture": "amd64", "Os": "linux"} as image] if image["Id"] == "sha256:" + fixture["config_sha256"]:
                pass
            case _:
                raise ValueError("Loaded image differs from the qualified fixture")
        command(["docker", "tag", image["Id"], tag])
        tag_installed = True
        records["image"] = {"id": image["Id"], "tag": tag, "fixture": fixture,
                            "workload": "source-built scratch sleep image; not Alpine"}

        # All source-bound builds complete before any timed startup. The ARM64
        # release is a cross-build, with no ARM64 runtime execution credit.
        builds = {}
        for side, revision, architecture in (("before", config["before_source"], "x86_64"),
                                             ("after", source, "x86_64"),
                                             ("arm64", source, "aarch64")):
            command(["git", "switch", "--detach", revision])
            check_source(revision)
            selected = [option for option in options if not option.startswith("--config=")]
            execution = private / (side + "-release-execution.json")
            try:
                built = invoke(side + "-release", ["build", "--config=" + architecture, *selected,
                               "--execution_log_json_file=" + str(execution), "//:release"], "build",
                               targets=["//:release"], source=revision)
            finally:
                record_execution(execution, architecture, side + "-release")
            if built["status"] != "PASS" or built["native_exit"] != 0:
                raise RuntimeError(side + " release build failed")
            owners = {report.Owner(row["label"], row["configuration"]) for row in built["expected"]}
            if len(owners) != 1 or next(iter(owners)).label != "//:release":
                raise ValueError("Unexpected release owner")
            outputs = report.advertised_build_outputs(evidence / ("invocation-" + side + "-release/events.jsonl"),
                                                       {(owner, "default") for owner in owners})
            by_name = {row["name"].split("/release/", 1)[1]: row for row in outputs}
            expected = set(config["runtime_paths"])
            if len(outputs) != 8 or set(by_name) != expected | {"containerd-shim-runsc-v1"}:
                raise ValueError("Unexpected release output set")
            for row in outputs:
                materialized(row)
            sentry = materialized(by_name["gvisor-bin/gvisor_sentry"]).read_bytes()
            if sentry[:6] != b"\x7fELF\x02\x01" or int.from_bytes(sentry[18:20], "little") != (183 if side == "arm64" else 62):
                raise ValueError("Configured release sentry has the wrong ELF architecture")
            retained = {}
            if side != "arm64":
                for name in config["runtime_paths"]:
                    destination = private / side / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(materialized(by_name[name]), destination)
                    destination.chmod(0o755)
                    retained[name] = report.file_record(destination)
            builds[side] = {"side": side, "source": revision, "architecture": architecture,
                            "release_outputs": outputs, "copied_files": retained,
                            "build": built, "status": "PASS"}
            records["runtime_builds"].append(builds[side])
        check_source(source)
        runtimes = {}
        with ExitStack() as scopes:
            for name, side, platform in (("before", "before", "systrap"),
                                          ("after", "after", "systrap"),
                                          ("ptrace", "after", "ptrace")):
                directory = evidence / (name + "-runtime")
                directory.mkdir()
                runtime_directory = private / side
                runtime_name = "runsc-platform-seccomp-" + name
                runtime = scopes.enter_context(prepared_runtime(directory / "runtime.json", runtime_directory, {
                    "runtime_name": runtime_name, "runtime_flags": ["--platform=" + platform, "--debug=false"],
                    "image_name": "benchmarks/alpine", "lifecycle_only": True, "cleanup_reserve_seconds": 90,
                    "retained_release_files": builds[side]["copied_files"],
                    "release_supplier": {"source": builds[side]["source"], "phase": side + "-release"},
                }))
                actual = {str(Path(row["path"]).relative_to(runtime_directory)): row for row in runtime["runtime_files"]}
                if actual.keys() != set(config["runtime_paths"]):
                    raise ValueError("Installed runtime omitted release files")
                for path, row in actual.items():
                    if any(row[key] != builds[side]["copied_files"][path][key] for key in ("bytes", "sha256")):
                        raise ValueError("Installed runtime differs from configured release outputs")
                runtimes[name] = {"name": runtime_name, "source": builds[side]["source"], "platform": platform,
                                  "files": runtime["runtime_files"],
                                  "configuration": runtime["config_after"]["runtimes"][runtime_name]}
            inputs = {"benchstat": benchstat, "runtimes": runtimes, "image": records["image"]}
            report.write_json(package / "inputs.json", inputs)
            report.write_json(evidence / "startup-inputs.json", inputs)
            selected = [*options, "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                        "--nozip_undeclared_test_outputs", "--test_sharding_strategy=disabled",
                        "--test_env=GO_TEST_WRAP_TESTV=1"]
            targets = ["//test/rbe/state_array_pair:paired_test"]
            # Build the owning py_test so its actual transitioned data dependency
            # supplies the common benchmark, including that dependency's runfiles.
            execution = private / "startup-harness-execution.json"
            try:
                built = invoke("startup-harness", ["build", *selected,
                               "--execution_log_json_file=" + str(execution), *targets], "build")
            finally:
                record_execution(execution, "x86_64", "startup-harness")
            if built["status"] != "PASS":
                raise RuntimeError("Declared startup harness build failed")
            query = 'filter("^(@@)?//test/benchmarks/base:startup_test$", labels(data, //test/rbe/state_array_pair:paired_test))'
            query_options = [*selected, "--universe_scope=//test/rbe/state_array_pair:paired_test"]
            for kind in ("jsonproto", "files"):
                queried = invoke("startup-dependency-" + kind,
                                 ["cquery", *query_options, "--output=" + kind, query],
                                 "command", targets=[])
                if queried["status"] != "PASS":
                    raise RuntimeError("Declared startup dependency query failed")
            graph = json.loads((evidence / "invocation-startup-dependency-jsonproto/stdout.log").read_text())
            match graph["results"]:
                case [{"target": {"rule": {"name": label}}, "configurationId": configuration_id}]:
                    if label.removeprefix("@@") != "//test/benchmarks/base:startup_test":
                        raise ValueError("Unexpected declared startup dependency owner")
                case _:
                    raise ValueError("Expected exactly one configured startup data dependency")
            configurations = [row for row in graph["configurations"] if row["id"] == configuration_id]
            if len(configurations) != 1 or not configurations[0]["checksum"]:
                raise ValueError("Missing startup dependency configuration")
            match (evidence / "invocation-startup-dependency-files/stdout.log").read_text().splitlines():
                case [path] if path.startswith("bazel-out/"):
                    binary = workspace / path
                case _:
                    raise ValueError("Expected exactly one declared startup executable")
            actual = report.file_record(binary)
            proof = json.loads((evidence / "x86_64-startup-harness-image-execution.json").read_text())
            if proof["status"] != "PASS":
                raise ValueError("Startup native action evidence is incomplete")
            producers = [action for action in proof["image_actions"] if any(
                output["path"] == path and output.get("digest", {}).get("hash") == actual["sha256"] and
                int(output.get("digest", {}).get("sizeBytes", -1)) == actual["bytes"]
                for output in action["actualOutputs"])]
            match producers:
                case [{"mnemonic": "GoLink", "exitCode": 0, "digest": {"hash": digest}} as action] if (
                        digest and not action["status"] and action["targetLabel"].removeprefix("@@") == label.removeprefix("@@")):
                    pass
                case _:
                    raise ValueError("Expected one successful GoLink producing the declared startup executable")
            inputs["harness"] = {"owner": label, "configuration": configurations[0],
                                 "path": path, "resolved_path": str(binary.resolve(strict=True)),
                                 "bytes": actual["bytes"], "sha256": actual["sha256"],
                                 "native_action": {key: action[key] for key in
                                     ("digest", "targetLabel", "runner", "cacheHit", "full_record_sha256")}}
            records["harness"] = inputs["harness"]
            report.write_json(package / "inputs.json", inputs)
            report.write_json(evidence / "startup-inputs.json", inputs)
            analysis = invoke("startup-analysis", ["test", *selected, "--nobuild", *targets], "analysis")
            if analysis["status"] != "PASS":
                raise RuntimeError("Startup test selection failed")
            tested = invoke("startup-paired", ["test", *selected, *targets], "test",
                            (evidence / "invocation-startup-analysis/events.jsonl",), retain_outputs=True)
            if tested["status"] != "PASS":
                raise RuntimeError("Startup cohort failed")
            # This is the actual loader diagnostic, not the unrelated sentry-wide
            # filter's precompilation message. Success crosses the source-bound
            # mandatory stub-render/install paths for the selected platform.
            for name, runtime in runtimes.items():
                matches = []
                for path in sorted((evidence / (name + "-runtime/runsc-logs")).glob("*")):
                    if not path.is_file():
                        continue
                    lines = [line for line in path.read_text().splitlines()
                             if re.search(r"loader.go:\d+\] Platform: " + runtime["platform"] + r"$", line)]
                    if lines:
                        matches.append({"path": str(path.relative_to(evidence)),
                                        **report.file_record(path), "lines": lines})
                if not matches:
                    raise ValueError("No actual loader platform observation for " + name)
                records["platform_logs"][name] = matches
        records["status"] = "PASS"
    finally:
        if records["status"] != "PASS":
            records["status"] = "FAIL"
        try:
            subprocess.run(["git", "switch", "--detach", source], check=True, timeout=30)
            if tag_installed:
                restore = ["docker", "tag", before_tag, tag] if before_tag else ["docker", "image", "rm", tag]
                subprocess.run(restore, check=True, capture_output=True, timeout=30)
            actual = sorted(subprocess.check_output(["docker", "ps", "-aq", "--no-trunc"], text=True, timeout=30).split())
            if initial_containers is not None and actual != initial_containers:
                raise ValueError("Startup changed the container inventory")
            records["cleanup"] = "PASS"
        except (OSError, ValueError, subprocess.SubprocessError):
            records["status"] = "FAIL"
            records["cleanup"] = "FAIL"
            raise
        finally:
            report.write_json(evidence / "startup-comparison.json", records)
