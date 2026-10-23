"""Compare startup using source-bound release sets and one fixed sleep image."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time
import urllib.request

import qualification_report as report
from image_runtime import prepared_runtime


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    source = config["source"]
    workspace = Path.cwd()
    lock = workspace / "MODULE.bazel.lock"
    candidate_lock = lock.read_bytes()
    package = workspace / "test/rbe/state_array_pair"
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

    try:
        # Reuse only the exact outputs of the completed source-bound builds.
        # The prior overall failure remains a failure: its first warmup failed.
        supplier = config["release_supplier"]
        archive = private / "release-evidence.tar.gz"
        fetch(supplier, archive)
        with tarfile.open(archive) as bundle:
            entries = [entry for entry in bundle if entry.isfile()]
            if len({entry.name for entry in entries}) != len(entries):
                raise ValueError("Duplicate retained evidence member")
            members = {entry.name: bundle.extractfile(entry).read() for entry in entries}
        previous = json.loads(members["startup-comparison.json"])
        prior_inputs = json.loads(members["startup-inputs.json"])
        if (previous["source"] != supplier["source"] or
                previous["before_source"] != config["before_source"] or
                previous["product"] != config["product"] or
                previous["qualifier_static_prerequisite"] != config["qualifier_static_prerequisite"] or
                previous["status"] != "FAIL" or previous["cleanup"] != "PASS"):
            raise ValueError("Retained source or failure evidence differs")
        records["release_supplier"] = supplier
        records["prior_status"] = previous["status"]
        shutil.copy2(archive, evidence / "retained-release-evidence.tar.gz")
        for name, expected_hash in config["matched_inputs"].items():
            if hashlib.sha256((workspace / name).read_bytes()).hexdigest() != expected_hash:
                raise ValueError("Shared startup source changed: " + name)
        harness = prior_inputs["harness"]
        common = json.loads(members["invocation-startup-harness/verdict.json"])
        if common["source"] != supplier["source"] or common["status"] != "PASS" or common["native_exit"] != 0:
            raise ValueError("Retained common harness build failed")
        events = evidence / "retained-harness-events.jsonl"
        events.write_bytes(members["invocation-startup-harness/events.jsonl"])
        owners = {report.Owner(row["label"], row["configuration"]) for row in common["expected"]}
        if len(owners) != 1 or next(iter(owners)).label != "//test/benchmarks/base:startup_test":
            raise ValueError("Unexpected retained harness owner")
        outputs = report.advertised_build_outputs(events, {(owner, "default") for owner in owners})
        if len(outputs) != 1 or any(outputs[0][key] != harness[key] for key in ("bytes", "sha256")):
            raise ValueError("Retained harness differs from its native BEP")
        benchstat = config["benchstat"]
        if prior_inputs["benchstat"] != benchstat:
            raise ValueError("Retained measurement tool differs")
        fetch(benchstat, package / "benchstat")
        (package / "benchstat").chmod(0o755)

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
            for side, revision in (("before", config["before_source"]), ("after", supplier["source"])):
                prior = next(row for row in previous["runtime_builds"] if row["side"] == side)
                verdict_path = prior["build"]["verdict"]
                built = json.loads(members[verdict_path])
                if (prior["source"] != revision or prior["status"] != "PASS" or
                        prior["execution_image"] != config["execution_image"] or
                        built["source"] != revision or built["status"] != "PASS" or built["native_exit"] != 0):
                    raise ValueError("Retained release build did not pass on its exact source")
                owners = {report.Owner(row["label"], row["configuration"]) for row in built["expected"]}
                if len(owners) != 1 or next(iter(owners)).label != "//:release":
                    raise ValueError("Unexpected retained release owner")
                events = evidence / ("retained-" + side + "-release-events.jsonl")
                events.write_bytes(members[verdict_path.replace("verdict.json", "events.jsonl")])
                release = report.advertised_build_outputs(events, {(owner, "default") for owner in owners})
                if len(release) != 8:
                    raise ValueError("Incomplete retained release outputs")
                for row in release:
                    matches = [old for old in prior["release_outputs"] if old["name"] == row["name"]]
                    if len(matches) != 1 or any(matches[0][key] != row[key] for key in ("identity", "uri", "bytes", "sha256")):
                        raise ValueError("Retained release output differs from its native BEP")
                by_name = {row["name"].split("/release/", 1)[1]: row for row in release}
                expected = set(config["runtime_paths"])
                if set(by_name) != expected | {"containerd-shim-runsc-v1"}:
                    raise ValueError("Unexpected retained release layout")
                destination = evidence / (side + "-runtime")
                destination.mkdir()
                runtime_directory = private / (side + "-runtime")
                retained = {}
                for name in sorted(expected):
                    row = by_name[name]
                    path = runtime_directory / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    fetch(dict(row, uri="bytestream://remote.buildbuddy.io/blobs/" + row["sha256"] + "/" + str(row["bytes"])), path)
                    path.chmod(0o755)
                    retained[name] = {key: row[key] for key in ("bytes", "sha256")}
                runtime_name = "runsc-compiler-startup-" + side
                runtime = scopes.enter_context(prepared_runtime(destination / "runtime.json", runtime_directory, {
                    "runtime_name": runtime_name, "runtime_flags": ["--platform=systrap"],
                    "image_name": "benchmarks/alpine", "lifecycle_only": True, "cleanup_reserve_seconds": 90,
                    "retained_release_files": retained, "release_supplier": {"invocation": supplier["invocation"], "source": revision},
                }))
                actual = {str(Path(row["path"]).relative_to(runtime_directory)): row for row in runtime["runtime_files"]}
                if actual.keys() != expected:
                    raise ValueError("Copied runtime omitted release files")
                for name, row in actual.items():
                    if any(row[key] != by_name[name][key] for key in ("bytes", "sha256")):
                        raise ValueError("Copied runtime differs from its configured release output")
                runtimes[side] = {"name": runtime_name, "source": revision,
                    "files": runtime["runtime_files"], "configuration": runtime["config_after"]["runtimes"][runtime_name]}
                records["runtime_builds"].append(dict(prior, reused=True, supplier=supplier["invocation"]))
            if command(["git", "rev-parse", "HEAD"]) != source:
                raise ValueError("Current qualifier source changed")
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
