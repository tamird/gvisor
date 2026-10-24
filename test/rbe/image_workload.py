"""Compare four existing Nginx benchmark groups with fixed image pairs."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time
import urllib.request
import uuid

import qualification_report as report
from image_runtime import materialize_retained_release, prepare_benchmark_helpers, prepared_runtime


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    workspace = Path.cwd()
    package = workspace / "test/rbe/image_pair"
    options = ["--config=x86_64", "--compilation_mode=opt", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--build_event_json_file_path_conversion=false",
               "--noannounce_rc"]
    selected_options = [*options, "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                "--nozip_undeclared_test_outputs", "--test_sharding_strategy=disabled",
                "--test_timeout=1500", "--test_env=GO_TEST_WRAP_TESTV=0"]
    tag = "gvisor.dev/images/benchmarks/nginx:latest"
    records = {"status": "INCOMPLETE", "source": config["source"], "product": config["product"],
               "commands": [], "observers": {}, "harnesses": {}, "groups": [], "cleanup": "INCOMPLETE"}
    owned = []
    original_tag = None
    installed_tag = False
    deadline = float(os.environ["APPLICABILITY_WORK_DEADLINE"])

    def remaining(reserve: int = 90) -> float:
        seconds = deadline - reserve - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Nginx paired qualification exhausted the work deadline")
        return seconds

    def command(name: str, argv: list[str], *, cleanup: bool = False) -> bytes:
        stdout, stderr = evidence / (name + ".stdout"), evidence / (name + ".stderr")
        row = {"name": name, "arguments": argv, "status": "INCOMPLETE"}
        records["commands"].append(row)
        try:
            with stdout.open("wb") as out, stderr.open("wb") as err:
                result = subprocess.run(argv, stdout=out, stderr=err,
                                        timeout=min(180, remaining(0 if cleanup else 90)))
            row["native_exit"] = result.returncode
            result.check_returncode()
            row["status"] = "PASS"
            return stdout.read_bytes()
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            row.update(status="FAIL", error=str(error))
            raise
        finally:
            for key, path in (("stdout", stdout), ("stderr", stderr)):
                if path.is_file():
                    row[key] = report.file_record(path)

    def fetch(record: dict, destination: Path) -> None:
        request = urllib.request.Request("https://app.buildbuddy.io/api/v1/GetFile",
            data=json.dumps({"uri": record["uri"]}).encode(),
            headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        with urllib.request.urlopen(request, timeout=min(180, remaining())) as response:
            data = response.read(record["bytes"] + 1)
        if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError("Retained supplier identity mismatch: " + str(destination))
        destination.write_bytes(data)

    def retained_image_archive(supplier: dict) -> Path:
        retained = private / "image-evidence.tar.gz"
        fetch(supplier["bundle"], retained)
        with tarfile.open(retained) as archive:
            entries = [entry for entry in archive if entry.isfile()]
            if len({entry.name for entry in entries}) != len(entries):
                raise ValueError("Duplicate supplier evidence member")
            members = {entry.name: archive.extractfile(entry).read() for entry in entries}
        built = json.loads(members["invocation-mixed-images/verdict.json"])
        if built["status"] != "PASS" or built["native_exit"] != 0 or built["source"] != supplier["source"]:
            raise ValueError("Retained image build is not source-bound PASS")
        events = evidence / "supplier-image-events.jsonl"
        events.write_bytes(members["invocation-mixed-images/events.jsonl"])
        owners = {report.Owner(row["label"], row["configuration"]) for row in built["expected"]}
        outputs = report.advertised_build_outputs(events, {(owner, "default") for owner in owners})
        archive_input = supplier["archive"]
        selected = [row for row in outputs if row["identity"]["label"] == "//images/benchmarks:qualification_nginx_archives"
                    and row["name"].endswith("qualification_nginx_archive_amd64.tar")]
        match selected:
            case [row] if all(row[key] == archive_input[key] for key in ("bytes", "sha256")):
                pass
            case _:
                raise ValueError("After image differs from the retained configured default output")
        contents = json.loads(members["nginx-contents.json"])
        amd64 = next(row for row in contents["architectures"] if row["architecture"] == "amd64")
        if contents["status"] != "PASS" or amd64["runtime_inputs"]["source"] != supplier["source"]:
            raise ValueError("After image lacks completed source-bound inspection")
        if amd64["config_sha256"] != supplier["config_sha256"] or any(
                amd64["archive"][key] != archive_input[key] for key in ("bytes", "sha256")):
            raise ValueError("After image supplier configuration differs")
        after_archive = private / "after-image.tar"
        fetch(archive_input, after_archive)
        return after_archive

    def image(reference: str, name: str) -> dict:
        rows = json.loads(command(name, ["docker", "image", "inspect", reference]))
        match rows:
            case [row] if row["Os"] == "linux" and row["Architecture"] == "amd64":
                return row
            case _:
                raise ValueError("Expected one Linux/AMD64 image: " + reference)


    def observe(side: str, image_id: str, runtime: str) -> dict:
        name = "qualification-nginx-observer-" + uuid.uuid4().hex
        owned.append(name)
        command(side + "-observer-create", ["docker", "create", "--name", name, "--runtime=" + runtime,
                                            image_id, "sleep", "infinity"])
        command(side + "-observer-start", ["docker", "start", name])
        [container] = json.loads(command(side + "-observer-inspect", ["docker", "inspect", name]))
        if container["Image"] != image_id or container["HostConfig"]["Runtime"] != runtime or not container["State"]["Running"]:
            raise ValueError("Actual Nginx observer image/runtime differs")
        command(side + "-observer-setup", ["docker", "exec", name, "sh", "-c",
                                           "mkdir -p /tmp/html && cp -a /local/* /tmp/html"])
        values = {}
        for key, arguments in (("version", ["-V"]), ("tmpfs", ["-T", "-c", "/etc/nginx/nginx.conf"]),
                               ("bind", ["-T", "-c", "/etc/nginx/nginx_gofer.conf"])):
            output = command(side + "-observer-" + key, ["docker", "exec", name, "nginx", *arguments])
            stderr = (evidence / (side + "-observer-" + key + ".stderr")).read_bytes()
            values[key] = (output + stderr).decode()
        if not values["version"].startswith("nginx version: nginx/1.25.1\n"):
            raise ValueError("Actual baseline/candidate Nginx version differs")
        paths = []
        for key in ("tmpfs", "bind"):
            included = re.findall(r"^# configuration file (/[^\n:]+):$", values[key], re.M)
            if not included:
                raise ValueError("Nginx did not identify its effective configuration: " + key)
            paths.extend(included)
        configurations = []
        for index, path in enumerate(dict.fromkeys(paths)):
            if ".." in Path(path).parts:
                raise ValueError("Invalid included configuration path")
            data = command(side + "-configuration-" + str(index), ["docker", "cp", "--follow-link", name + ":" + path, "-"])
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                entries = [item for item in archive if item.isfile()]
                match entries:
                    case [entry]:
                        with archive.extractfile(entry) as stream:
                            contents = stream.read()
                    case _:
                        raise ValueError("Expected exactly one included configuration file: " + path)
            retained = evidence / (side + "-configuration-" + str(index) + ".conf")
            retained.write_bytes(contents)
            configurations.append({"image_path": path, "member": retained.name, **report.file_record(retained)})
        command(side + "-observer-remove", ["docker", "rm", "--force", name])
        owned.remove(name)
        return {"image": image_id, "runtime": runtime, "diagnostics": values,
                "ordered_includes": paths, "configurations": configurations}

    def bind_harness(group: dict, proof: dict) -> dict:
        target, harness_label = group["target"], group["owner"]
        phase = "paired-" + group["name"] + "-dependency-"
        query = 'filter("^(@@)?' + harness_label + '$", labels(data, ' + target + '))'
        for kind in ("jsonproto", "files"):
            result = invoke(phase + kind, ["cquery", *selected_options, "--universe_scope=" + target,
                            "--output=" + kind, query], "command", targets=[])
            if result["status"] != "PASS":
                raise RuntimeError("Actual benchmark data dependency query failed")
        graph = json.loads((evidence / ("invocation-" + phase + "jsonproto/stdout.log")).read_text())
        match graph["results"]:
            case [{"target": {"rule": {"name": label}}, "configurationId": configuration_id}] if label.removeprefix("@@") == harness_label:
                pass
            case _:
                raise ValueError("Expected one declared benchmark data dependency")
        configurations = [row for row in graph["configurations"] if row["id"] == configuration_id]
        if len(configurations) != 1 or not configurations[0]["checksum"]:
            raise ValueError("Missing actual benchmark configuration")
        match (evidence / ("invocation-" + phase + "files/stdout.log")).read_text().splitlines():
            case [path] if path.startswith("bazel-out/"):
                binary = workspace / path
            case _:
                raise ValueError("Expected one actual benchmark executable")
        actual = report.file_record(binary)
        producers = [action for action in proof["image_actions"] if any(
            out["path"] == path and out.get("digest", {}).get("hash") == actual["sha256"] and
            int(out.get("digest", {}).get("sizeBytes", -1)) == actual["bytes"] for out in action["actualOutputs"])]
        match producers:
            case [{"mnemonic": "GoLink", "exitCode": 0, "digest": {"hash": digest}} as action] if digest and not action["status"] and action["targetLabel"].removeprefix("@@") == harness_label:
                pass
            case _:
                raise ValueError("Expected one exact GoLink for the declared benchmark")
        return {"owner": label, "configuration": configurations[0], "path": path,
                "resolved_path": str(binary.resolve(strict=True)), **actual,
                "native_action": {key: action[key] for key in ("digest", "targetLabel", "runner", "cacheHit", "full_record_sha256")}}

    try:
        checked = invoke("reporting-contract", ["run", *options, "@rules_python//python/bin:python", "--", "-c",
            'import os, subprocess, sys; os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"]); sys.exit(subprocess.run([sys.executable, "test/rbe/image_pair/reporting_test.py", "-v"]).returncode)'],
            "command", targets=[])
        contract = (evidence / "invocation-reporting-contract/stderr.log").read_text()
        names = ("test_native_report", "test_missing_and_duplicate_rows", "test_failed_and_late_rows",
                 "test_wrong_iterations_and_metrics", "test_balanced_fixed_cohort")
        if checked["status"] != "PASS" or "Ran 5 tests" not in contract:
            raise RuntimeError("Native benchmark reporting contract checks failed")
        for name in names:
            if not any(line.startswith(name + " (") and line.endswith(" ... ok") for line in contract.splitlines()):
                raise ValueError("Missing positive benchmark reporting control: " + name)
        records["containers_before"] = sorted(command("containers-before", ["docker", "ps", "-aq", "--no-trunc"]).decode().split())
        tags = command("image-tags-before", ["docker", "image", "ls", "--format={{.Repository}}:{{.Tag}}"]).decode().splitlines()
        if tag in tags:
            original_tag = image(tag, "original-tag")["Id"]
        fetch(config["benchstat"], package / "benchstat")
        (package / "benchstat").chmod(0o755)
        report.write_json(package / "inputs.json", {})
        supplier = config["image_supplier"]
        after_archive = retained_image_archive(supplier)
        installed_tag = True
        command("after-image-load", ["docker", "load", "--input", str(after_archive)])
        after = image("sha256:" + supplier["config_sha256"], "after-image")
        command("before-image-pull", ["docker", "pull", config["before_image"]["immutable_image"]])
        before = image(config["before_image"]["immutable_image"], "before-image")
        if before["Id"] != config["before_image"]["config_id"] or after["Id"] != "sha256:" + supplier["config_sha256"]:
            raise ValueError("Immutable image arm identity differs")
        command("install-current-tag", ["docker", "tag", after["Id"], tag])
        records["image_supplier"] = supplier
        records["images"] = {"before": before, "after": after}
        runtime_directory = private / "nginx-paired-runtime"
        retained_files = materialize_retained_release(config["runtime_supplier"], runtime_directory, evidence, "nginx-paired")
        records["runtime_source"] = config["runtime_supplier"]["source"]
        helpers = prepare_benchmark_helpers(evidence / "benchmark-helper-images.json",
                                            private / "benchmark-helper-images", config["benchmark_utility"])
        groups = config["groups"]
        if [group["name"] for group in groups] != ["serving", "lifecycle", "size", "usage"]:
            raise ValueError("Expected the four declared Nginx benchmark groups")
        build_targets = [group["target"] for group in groups]
        if build_targets != config["targets"]:
            raise ValueError("Paired build/test owner selection differs")
        execution = private / "paired-harness-execution.json"
        try:
            built = invoke("paired-harness", ["build", *selected_options, "--execution_log_json_file=" + str(execution),
                           "--noexecution_log_sort", *build_targets], "build", targets=build_targets)
        finally:
            record_execution(execution, "x86_64", "paired-harness")
        if built["status"] != "PASS":
            raise RuntimeError("Common paired harness build failed")
        proof = json.loads((evidence / "x86_64-paired-harness-image-execution.json").read_text())
        if proof["status"] != "PASS":
            raise ValueError("Common harness native action evidence is incomplete")
        for group in groups:
            records["harnesses"][group["name"]] = bind_harness(group, proof)
        runtime_name = "qualification-nginx-paired-" + uuid.uuid4().hex
        with prepared_runtime(evidence / "paired-runtime.json", runtime_directory, {
                "runtime_name": runtime_name, "runtime_flags": ["--platform=systrap", "--network=sandbox"],
                "image_name": "benchmarks/nginx", "lifecycle_only": True, "cleanup_reserve_seconds": 90,
                "docker_experimental": True, "docker_version": "24.0.9",
                "retained_release_files": retained_files, "release_supplier": config["runtime_supplier"]}) as prepared:
            if not prepared["server_info"]["ExperimentalBuild"]:
                raise ValueError("Nginx checkpoint comparisons require Docker experimental mode")
            files = {str(Path(row["path"]).relative_to(runtime_directory)): row for row in prepared["runtime_files"]}
            if files.keys() != retained_files.keys() or any(any(files[name][key] != expected[key] for key in ("bytes", "sha256")) for name, expected in retained_files.items()):
                raise ValueError("Installed common runtime differs from the retained release")
            runtime = {"name": runtime_name, "files": prepared["runtime_files"],
                       "configuration": prepared["config_after"]["runtimes"][runtime_name]}
            for side, arm in (("before", before), ("after", after)):
                records["observers"][side] = observe(side, arm["Id"], runtime_name)
            for group in groups:
                name, target = group["name"], group["target"]
                inputs = {"harness": records["harnesses"][name], "benchstat": config["benchstat"],
                    "runtimes": {"before": runtime, "after": runtime},
                    "image": {"tag": tag, "before": {"id": before["Id"]}, "after": {"id": after["Id"]}},
                    "helpers": [{"tag": helpers[helper]["name"], "id": helpers[helper]["image"]["Id"]} for helper in ("hey", "util")],
                    "benchmark": group["benchmark"], "child_timeout_seconds": 300}
                report.write_json(package / "inputs.json", inputs)
                report.write_json(evidence / ("paired-" + name + "-inputs.json"), inputs)
                row = {"name": name, "target": target, "status": "INCOMPLETE"}
                records["groups"].append(row)
                phase = "paired-" + name
                analysis = invoke(phase + "-analysis", ["test", *selected_options, "--nobuild", target], "analysis", targets=[target])
                if analysis["status"] != "PASS":
                    raise RuntimeError("Paired test analysis failed: " + name)
                tested = invoke(phase + "-test", ["test", *selected_options, target], "test",
                                (evidence / ("invocation-" + phase + "-analysis/events.jsonl"),), targets=[target], retain_outputs=True)
                row["status"] = tested["status"]
                if tested["status"] != "PASS":
                    raise RuntimeError("Fixed16 paired Nginx group failed: " + name)
                inventory = sorted(command(phase + "-containers", ["docker", "ps", "-aq", "--no-trunc"]).decode().split())
                if inventory != records["containers_before"]:
                    raise RuntimeError("Paired group leaked containers: " + name)
        records["status"] = "PASS"
    finally:
        if records["status"] != "PASS":
            records["status"] = "FAIL"
        failures = []
        cleanup_actions = [("cleanup-" + name, ["docker", "rm", "--force", name]) for name in owned]
        if installed_tag:
            restore = ["docker", "tag", original_tag, tag] if original_tag else ["docker", "image", "rm", tag]
            cleanup_actions.append(("restore-image-tag", restore))
        for name, argv in cleanup_actions:
            try:
                command(name, argv, cleanup=True)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                failures.append({"action": name, "error": str(error)})
        try:
            after_containers = command("containers-after", ["docker", "ps", "-aq", "--no-trunc"], cleanup=True).decode().split()
            records["containers_after"] = sorted(after_containers)
            if "containers_before" in records and records["containers_before"] != records["containers_after"]:
                raise RuntimeError("Nginx paired preparation changed container inventory")
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            failures.append({"action": "containers-after", "error": str(error)})
        records["cleanup_errors"] = failures
        records["cleanup"] = "FAIL" if failures else "PASS"
        if failures:
            records["status"] = "FAIL"
        report.write_json(evidence / "nginx-paired-comparison.json", records)
        if failures:
            raise RuntimeError("Nginx paired cleanup failed: " + json.dumps(failures))
