"""Compare startup using source-bound release sets and one fixed sleep image."""
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

import qualification_report as report
from image_runtime import prepared_runtime


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    source = config["source"]
    workspace = Path.cwd()
    lock = workspace / "MODULE.bazel.lock"
    candidate_lock = lock.read_bytes()
    package = workspace / "test/rbe/state_array_pair"
    staged = private / "common-harness"
    staged.mkdir()
    options = ["--config=x86_64", "--compilation_mode=opt", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--build_event_json_file_path_conversion=false",
               "--noannounce_rc"]
    records = {"status": "INCOMPLETE", "source": source, "product": config["product"],
               "before_source": config["before_source"],
               "qualifier_static_prerequisite": config["qualifier_static_prerequisite"],
               "runtime_builds": []}
    before_tag = None
    tag_installed = False
    tag = "gvisor.dev/images/benchmarks/alpine:latest"

    def remaining():
        value = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic() - 90
        if value <= 0:
            raise TimeoutError("Startup qualification exhausted its work deadline")
        return value

    def command(argv):
        return subprocess.check_output(argv, text=True, timeout=min(60, remaining())).strip()

    def fetch(record, destination):
        request = urllib.request.Request(
            "https://app.buildbuddy.io/api/v1/GetFile",
            data=json.dumps({"uri": record["uri"]}).encode(),
            headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        with urllib.request.urlopen(request, timeout=min(60, remaining())) as response:
            data = response.read(record["bytes"] + 1)
        if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError("Source-built input identity mismatch: " + str(destination))
        destination.write_bytes(data)

    def outputs(result, directory, expected):
        if result["status"] != "PASS" or result["native_exit"]:
            raise RuntimeError("Source-bound build failed")
        owners = {report.Owner(row["label"], row["configuration"]) for row in result["expected"]}
        if len(owners) != len(expected) or {owner.label for owner in owners} != set(expected):
            raise ValueError("Unexpected configured build owners")
        rows = report.advertised_build_outputs(directory / "events.jsonl", {(owner, "default") for owner in owners})
        for row in rows:
            uri = urlsplit(row["uri"])
            if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
                raise ValueError("Build output was not materialized")
            path = Path(unquote(uri.path)).resolve(strict=True)
            path.relative_to(output_root.resolve())
            if report.file_record(path) != {"bytes": row["bytes"], "sha256": row["sha256"]}:
                raise ValueError("Materialized output differs from BEP")
            row["materialized_path"] = str(path)
        return rows

    try:
        common_targets = ["//test/benchmarks/base:startup_test"]
        built = invoke("startup-harness", ["build", *options, *common_targets], "build", targets=common_targets)
        common = outputs(built, evidence / "invocation-startup-harness", common_targets)
        if len(common) != 1:
            raise ValueError("Expected one configured startup harness")
        harness = common[0]
        shutil.copy2(harness["materialized_path"], staged / "startup_test")
        # Reuse the source-built measurement tool without changing either
        # runtime's dependency graph just to build the reporter.
        benchstat = config["benchstat"]
        fetch(benchstat, staged / "benchstat")
        (staged / "benchstat").chmod(0o755)

        fixture = config["fixture"]
        archive = private / "sleep-image.tar"
        fetch(fixture, archive)
        result = subprocess.run(["docker", "image", "inspect", tag, "--format={{.Id}}"],
                                capture_output=True, text=True, timeout=min(30, remaining()))
        if result.returncode == 0:
            before_tag = result.stdout.strip()
        else:
            # A daemon failure must not be mistaken for an absent image tag.
            command(["docker", "info", "--format={{.ID}}"])
            if tag in command(["docker", "image", "ls", "--format={{.Repository}}:{{.Tag}}"]).splitlines():
                raise RuntimeError("Existing image tag could not be inspected")
        command(["docker", "load", "--input", str(archive)])
        image = json.loads(command(["docker", "image", "inspect", "sha256:" + fixture["config_sha256"]]))[0]
        if image["Architecture"] != "amd64" or image["Id"] != "sha256:" + fixture["config_sha256"]:
            raise ValueError("Loaded image differs from the qualified fixture")
        command(["docker", "tag", image["Id"], tag])
        tag_installed = True
        records["image"] = {"id": image["Id"], "tag": tag, "fixture": fixture,
                            "workload": "source-built scratch sleep image; not Alpine"}
        runtimes = {}
        with ExitStack() as scopes:
            for side, revision in (("before", config["before_source"]), ("after", source)):
                runtime_record = {"side": side, "source": revision, "status": "INCOMPLETE",
                                  "execution_image": config["execution_image"]}
                records["runtime_builds"].append(runtime_record)
                command(["git", "switch", "--detach", revision])
                for name, expected_hash in config["matched_inputs"].items():
                    if hashlib.sha256((workspace / name).read_bytes()).hexdigest() != expected_hash:
                        raise ValueError("Shared startup source changed: " + name)
                if side == "after":
                    lock.write_bytes(candidate_lock)
                prepared = invoke(side + "-release-preparation", ["cquery", "--config=x86_64",
                    "--compilation_mode=opt", "--lockfile_mode=update", "--noannounce_rc",
                    "--output=label", "//:release"], "command", targets=[], source=revision)
                runtime_record["preparation"] = {key: prepared[key] for key in ("status", "native_exit")}
                if prepared["status"] != "PASS":
                    raise RuntimeError("Release dependency preparation failed")
                built = invoke(side + "-release", ["build", *options, "//:release"], "build",
                               targets=["//:release"], source=revision)
                directory = evidence / ("invocation-" + side + "-release") if side == "after" else evidence / "source-builds" / (side + "-release")
                runtime_record["build"] = {key: built[key] for key in ("status", "native_exit", "source", "expected")}
                runtime_record["build"]["verdict"] = str((directory / "verdict.json").relative_to(evidence))
                release = outputs(built, directory, ["//:release"])
                by_name = {}
                for row in release:
                    _, separator, name = row["materialized_path"].rpartition("/release/")
                    if not separator or name in by_name:
                        raise ValueError("Unexpected release output layout")
                    by_name[name] = row
                destination = evidence / (side + "-runtime")
                destination.mkdir()
                runtime_directory = private / (side + "-runtime")
                runtime_name = "runsc-compiler-startup-" + side
                runtime = scopes.enter_context(prepared_runtime(destination / "runtime.json", runtime_directory, {
                    "runtime_name": runtime_name, "runtime_flags": ["--platform=systrap"],
                    "image_name": "benchmarks/alpine", "lifecycle_only": True, "cleanup_reserve_seconds": 90,
                }))
                expected = set(config["runtime_paths"])
                if not expected <= by_name.keys():
                    raise ValueError("Configured release omitted a required runtime file")
                actual = {str(Path(row["path"]).relative_to(runtime_directory)): row for row in runtime["runtime_files"]}
                if actual.keys() != expected:
                    raise ValueError("Copied runtime omitted release files")
                for name, row in actual.items():
                    if any(row[key] != by_name[name][key] for key in ("bytes", "sha256")):
                        raise ValueError("Copied runtime differs from its configured release output")
                runtimes[side] = {"name": runtime_name, "source": revision,
                    "files": runtime["runtime_files"], "configuration": runtime["config_after"]["runtimes"][runtime_name]}
                runtime_record.update({"status": "PASS", "release_outputs": release})
            if command(["git", "rev-parse", "HEAD"]) != source:
                raise ValueError("Candidate source was not restored")
            shutil.copy2(staged / "startup_test", package / "startup_test")
            shutil.copy2(staged / "benchstat", package / "benchstat")
            inputs = {"harness": {key: harness[key] for key in ("bytes", "sha256")},
                      "benchstat": benchstat,
                      "runtimes": runtimes, "image": records["image"]}
            report.write_json(package / "inputs.json", inputs)
            report.write_json(evidence / "startup-inputs.json", inputs)
            selected = [*options, "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                        "--nozip_undeclared_test_outputs", "--test_sharding_strategy=disabled",
                        "--test_env=GO_TEST_WRAP_TESTV=1"]
            targets = ["//test/rbe/state_array_pair:paired_test"]
            analysis = invoke("startup-analysis", ["test", *selected, "--nobuild", *targets], "analysis")
            if analysis["status"] != "PASS":
                raise RuntimeError("Startup test selection failed")
            tested = invoke("startup-paired", ["test", *selected, *targets], "test",
                            (evidence / "invocation-startup-analysis/events.jsonl",))
            if tested["status"] != "PASS":
                raise RuntimeError("Startup cohort failed")
        records["status"] = "PASS"
    finally:
        if records["status"] != "PASS":
            records["status"] = "FAIL"
        try:
            subprocess.run(["git", "switch", "--detach", source], check=True, timeout=30)
            lock.write_bytes(candidate_lock)
            if tag_installed:
                restore = ["docker", "tag", before_tag, tag] if before_tag else ["docker", "image", "rm", tag]
                subprocess.run(restore, check=True, capture_output=True, timeout=30)
            records["cleanup"] = "PASS"
        except (OSError, subprocess.SubprocessError):
            records["status"] = "FAIL"
            records["cleanup"] = "FAIL"
            raise
        finally:
            report.write_json(evidence / "startup-comparison.json", records)
