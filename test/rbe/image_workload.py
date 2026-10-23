"""Pilot the six existing parallel iperf3 cases on source-matched runtimes."""
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
import uuid

import qualification_report as report
from image_runtime import prepared_runtime


BENCHMARK = "//test/benchmarks/network:iperf_test"
RELEASE = "//:release"
NAMES = {f"BenchmarkIperfManyConnections/operation.{direction}/parallel.{parallel}": parallel
         for direction in ("Upload", "Download") for parallel in (4, 16, 64)}


def observations(log: str, iterations: int) -> list[dict]:
    rows = [json.loads(line.split("IPERF_RESULT ", 1)[1]) for line in log.splitlines() if "IPERF_RESULT " in line]
    measured = [row for row in rows if row["Iterations"] == iterations]
    if Counter(row["Name"] for row in measured) != Counter(NAMES.keys()):
        raise ValueError("Pilot omitted or duplicated a measured iperf3 case")
    for row in rows:
        parallel = NAMES[row["Name"]]
        requested = row["Iterations"] * 1024
        data = row["Output"]
        start, end = data["start"], data["end"]
        settings, summary = start["test_start"], end["sum_sent"]
        connected = [stream["socket"] for stream in start["connected"]]
        streams = end["streams"]
        sent = [stream["sender"] for stream in streams]
        if (row["Iterations"] not in (1, iterations) or row["Parallel"] != parallel or data.get("error")
                or settings["num_streams"] != parallel or settings["bytes"] != requested
                or settings["blksize"] != 131072 or settings["protocol"] != "TCP"
                or len(connected) != parallel or len(set(connected)) != parallel
                or len(sent) != parallel or {item["socket"] for item in sent} != set(connected)):
            raise ValueError("Actual iperf3 stream/request identity differs")
        if summary["bytes"] != sum(item["bytes"] for item in sent) or summary["bytes"] < requested:
            raise ValueError("iperf3 sender summary is not the complete aggregate transfer")
        if any(not math.isfinite(summary[key]) or summary[key] <= 0 for key in ("seconds", "bits_per_second")):
            raise ValueError("iperf3 omitted finite positive transfer timing")
        row["zero_byte_streams"] = [item["socket"] for item in sent if item["bytes"] == 0]
        # Calibration may finish before every connected stream sends. The
        # measured many-stream workload must actually use every connection.
        if row["Iterations"] == iterations and row["zero_byte_streams"]:
            raise ValueError("Measured pilot did not send data on every requested stream")
    return rows


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    source = config["source"]
    workspace = Path.cwd()
    tag = "gvisor.dev/images/benchmarks/iperf:latest"
    options = ["--config=x86_64", "--compilation_mode=opt", "--lockfile_mode=error",
               "--extra_toolchains=@llvm//toolchain:all", "--remote_download_outputs=all", "--noannounce_rc"]
    records = {"source": source, "status": "INCOMPLETE", "builds": {}, "samples": [],
               "commands": [], "cleanup": "INCOMPLETE", "cleanup_errors": []}
    original_tag = None
    tag_changed = False
    deadline = float(os.environ["APPLICABILITY_WORK_DEADLINE"])

    def command(name: str, arguments: list[str], cleanup: bool = False) -> str:
        row = {"name": name, "arguments": arguments, "status": "INCOMPLETE"}
        records["commands"].append(row)
        try:
            remaining = deadline - time.monotonic() - (0 if cleanup else 90)
            if remaining <= 0:
                raise TimeoutError("Pilot work deadline exhausted")
            result = subprocess.run(arguments, text=True, capture_output=True, timeout=min(180, remaining))
            row.update(native_exit=result.returncode, stdout=result.stdout, stderr=result.stderr)
            result.check_returncode()
            row["status"] = "PASS"
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError, TimeoutError) as error:
            row.update(status="FAIL", error=str(error))
            raise

    def check_source(revision: str, cleanup: bool = False) -> None:
        if command("source-head", ["git", "rev-parse", "HEAD"], cleanup=cleanup) != revision:
            raise ValueError("Unexpected source revision")
        if command("source-status", ["git", "status", "--porcelain", "--untracked-files=no"], cleanup=cleanup):
            raise ValueError("Tracked source changed")
        for name, expected in config["matched_inputs"].items():
            path = workspace / name
            data = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("Common fixture/build input changed: " + name)

    def inspect() -> dict:
        match json.loads(command("image-inspect", ["docker", "image", "inspect", tag])):
            case [item] if item["Architecture"] == "amd64" and item["Os"] == "linux":
                return item
            case _:
                raise ValueError("Expected one Linux AMD64 iperf3 image")

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
        records["host"] = {"cpuinfo": Path("/proc/cpuinfo").read_text(),
                           "affinity": sorted(os.sched_getaffinity(0)),
                           "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                           "uname": command("uname", ["uname", "-a"]),
                           "docker": json.loads(command("docker-info", ["docker", "info", "--format={{json .}}"])),
                           "runc": command("runc-version", ["runc", "--version"])}
        records["containers_before"] = sorted(command("containers-before", ["docker", "ps", "-aq", "--no-trunc"]).split())
        tags = command("tags-before", ["docker", "image", "ls", "--format={{.Repository}}:{{.Tag}}"]).splitlines()
        if tag in tags:
            original_tag = inspect()["Id"]
        supplier = config["image_supplier"]
        # The old supplier artifacts have expired from CAS. Regenerate only
        # its archive from the exact qualified source, then require the same
        # archive bytes and image config before either arm can use it.
        lock = workspace / "MODULE.bazel.lock"
        current_lock = lock.read_bytes()
        records["supplier_build"] = {"source": supplier["source"], "status": "INCOMPLETE",
                                     "current_lock": report.file_record(lock)}
        try:
            command("switch-supplier", ["git", "switch", "--detach", supplier["source"]])
            lock.unlink()
            supplier_options = ["--config=x86_64", "--compilation_mode=opt", "--noannounce_rc"]
            supplier_target = "//images/benchmarks:iperf_archive"
            preparation_directory = evidence / "source-builds/supplier-preparation"
            prepared = invoke("supplier-preparation", ["cquery", *supplier_options,
                "--lockfile_mode=update", "--output=label", supplier_target], "command",
                targets=[], source=supplier["source"], directory=preparation_directory)
            if prepared["status"] != "PASS" or prepared["native_exit"] != 0 or not lock.is_file():
                raise RuntimeError("Recorded iperf3 supplier preparation failed")
            supplier_directory = evidence / "source-builds/supplier"
            built = invoke("supplier-archive", ["build", *supplier_options,
                "--lockfile_mode=error", "--remote_download_outputs=all", supplier_target], "build",
                targets=[supplier_target], source=supplier["source"], directory=supplier_directory)
            if built["status"] != "PASS" or built["native_exit"] != 0:
                raise RuntimeError("Recorded iperf3 supplier archive build failed")
            [owner] = [report.Owner(row["label"], row["configuration"]) for row in built["expected"]]
            if owner.label != supplier_target:
                raise ValueError("Unexpected regenerated iperf3 archive owner")
            [archive_output] = report.advertised_build_outputs(supplier_directory / "events.jsonl", {(owner, "default")})
            if any(archive_output[key] != supplier["archive"][key] for key in ("name", "sha256", "bytes")):
                raise ValueError("Regenerated iperf3 archive differs from the qualified bytes")
            archive_path = private / "iperf3.tar"
            shutil.copyfile(materialized(archive_output), archive_path)
            records["supplier_build"].update(status="PASS", archive=report.file_record(archive_path),
                                             supplier_lock=report.file_record(lock))
        finally:
            try:
                command("restore-supplier-source", ["git", "switch", "--detach", source], cleanup=True)
            finally:
                lock.write_bytes(current_lock)
                records["supplier_build"]["restored_lock"] = report.file_record(lock)
            check_source(source)
            if records["supplier_build"]["restored_lock"] != records["supplier_build"]["current_lock"]:
                raise ValueError("Supplier regeneration changed the current source lock")
        tag_changed = True
        command("image-load", ["docker", "load", "--input", str(archive_path)])
        command("image-tag", ["docker", "tag", supplier["config_id"], tag])
        if inspect()["Id"] != supplier["config_id"]:
            raise ValueError("Loaded iperf3 image config differs")
        records["image_supplier"] = {**supplier, "default_output": archive_output}

        # Validate the actual genrule compiler selection before either build;
        # these rules use Make variables, unlike ordinary CcInfo consumers.
        query = command("release-genrules", ["bazel", "--bazelrc=" + os.environ["APPLICABILITY_AUTH_RC"],
            "aquery", *options, "--output=jsonproto", 'mnemonic("Genrule", deps(//:release))'])
        (evidence / "release-genrules.json").write_text(query + "\n")
        graph = json.loads(query)
        labels = {row["id"]: row["label"] for row in graph["targets"]}
        fragments = {row["id"]: row for row in graph["pathFragments"]}

        def artifact_path(fragment_id: int) -> str:
            parts = []
            while fragment_id:
                fragment = fragments[fragment_id]
                parts.append(fragment["label"])
                fragment_id = fragment.get("parentId", 0)
            return "/".join(reversed(parts))

        artifacts = {row["id"]: artifact_path(row["pathFragmentId"]) for row in graph["artifacts"]}
        [vdso] = [row for row in graph["actions"] if labels[row["targetId"]] == "//vdso:vdso"
                  and [artifacts[item] for item in row["outputIds"]] == ["bazel-out/k8-opt/bin/vdso/vdso.so"]]
        deps = {row["id"]: row for row in graph["depSetOfFiles"]}
        pending, visited, inputs = list(vdso["inputDepSetIds"]), set(), set()
        while pending:
            key = pending.pop()
            if key not in visited:
                visited.add(key)
                inputs.update(artifacts[item] for item in deps[key].get("directArtifactIds", []))
                pending.extend(deps[key].get("transitiveDepSetIds", []))
        arguments = shlex.split(vdso["arguments"][-1])
        [compiler] = [item for item in arguments if item.startswith("external/") and Path(item).name == "clang++"]
        [linker] = [item.removeprefix("--ld-path=") for item in arguments if item.startswith("--ld-path=")]
        if (vdso["mnemonic"] != "Genrule" or compiler not in inputs or linker not in inputs
                or Path(linker).name != "ld.lld" or "--sysroot=/dev/null" not in arguments
                or arguments[arguments.index("-target") + 1] != "x86_64-linux-gnu"
                or [artifacts[item] for item in vdso["outputIds"]] != ["bazel-out/k8-opt/bin/vdso/vdso.so"]):
            raise ValueError("vDSO does not select the declared target compiler/linker and output")
        records["genrule_preflight"] = {"owner": "//vdso:vdso", "compiler": compiler, "linker": linker,
            "action": vdso, "identity": report.file_record(evidence / "release-genrules.json")}

        # Build both arms before any timed consumer starts. Source and all
        # common fixture/toolchain inputs are checked again on each switch.
        for side, revision in (("before", config["before_source"]), ("after", source)):
            command("switch-" + side, ["git", "switch", "--detach", revision])
            check_source(revision)
            directory = evidence / "source-builds" / side
            result = invoke(side + "-build", ["build", *options, BENCHMARK, RELEASE], "build",
                            targets=[BENCHMARK, RELEASE], source=revision, directory=directory)
            if result["status"] != "PASS" or result["native_exit"] != 0:
                raise RuntimeError(side + " source-matched runtime/benchmark build failed")
            owners = {report.Owner(row["label"], row["configuration"]) for row in result["expected"]}
            if {owner.label for owner in owners} != {BENCHMARK, RELEASE} or len(owners) != 2:
                raise ValueError("Expected the exact benchmark and release owners")
            outputs = report.advertised_build_outputs(directory / "events.jsonl", {(owner, "default") for owner in owners})
            release = [row for row in outputs if row["identity"]["label"] == RELEASE]
            [binary] = [row for row in outputs if row["identity"]["label"] == BENCHMARK]
            materialized(binary)
            by_name = {row["name"].split("/release/", 1)[1]: row for row in release}
            if len(release) != 8 or set(by_name) != set(config["runtime_paths"]) | {"containerd-shim-runsc-v1"}:
                raise ValueError("Source-matched release output set differs")
            retained = {}
            for name in config["runtime_paths"]:
                destination = private / side / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(materialized(by_name[name]), destination)
                destination.chmod(0o755)
                retained[name] = report.file_record(destination)
            records["builds"][side] = {"source": revision, "outputs": outputs, "runtime_files": retained, "benchmark": binary}
            report.write_json(directory / "copied-outputs.json", outputs)

        iterations = config["iterations"]
        manifest = evidence / "iperf-declared-benchmarks.json"
        report.write_json(manifest, {"owners": {BENCHMARK: [
            {"name": name, "count": 1, "iterations": iterations,
             "units": ["ns/op", "MB/s", "bandwidth.bytes_per_second"]} for name in sorted(NAMES)]}})
        for side, revision in (("before", config["before_source"]), ("after", source)):
            command("switch-" + side, ["git", "switch", "--detach", revision])
            check_source(revision)
            directory = evidence / side
            directory.mkdir()
            runtime = "qualification-netip-" + side + "-" + uuid.uuid4().hex
            configuration = {"runtime_name": runtime, "runtime_flags": ["--platform=systrap", "--network=sandbox"],
                "image_name": "benchmarks/iperf", "lifecycle_only": True, "cleanup_reserve_seconds": 90,
                "retained_release_files": records["builds"][side]["runtime_files"],
                "release_supplier": {"source": revision, "phase": side + "-build"}}
            with prepared_runtime(directory / "runtime.json", private / side, configuration) as prepared:
                if inspect()["Id"] != supplier["config_id"]:
                    raise ValueError("Pilot image changed between arms")
                selected = [*options, "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                    "--test_sharding_strategy=disabled", "--nozip_undeclared_test_outputs", "--test_timeout=600",
                    "--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=0", "--test_arg=-test.v=false",
                    "--test_arg=-test.run=^$", "--test_arg=-test.bench=^BenchmarkIperfManyConnections$",
                    "--test_arg=-test.benchtime=" + str(iterations) + "x", "--test_arg=-test.count=1",
                    "--test_arg=-runtime=" + runtime]
                analysis_directory = (evidence / "source-builds/before/pilot-analysis" if side == "before"
                                      else evidence / "invocation-after-pilot-analysis")
                test_directory = (evidence / "source-builds/before/pilot" if side == "before"
                                  else evidence / "invocation-after-pilot")
                analysis = invoke(side + "-pilot-analysis", ["test", *selected, "--nobuild", BENCHMARK], "analysis",
                                  targets=[BENCHMARK], source=revision, directory=analysis_directory)
                if analysis["status"] != "PASS":
                    raise RuntimeError(side + " pilot analysis failed")
                tested = invoke(side + "-pilot", ["test", *selected, BENCHMARK], "test",
                    (analysis_directory / "events.jsonl",),
                    targets=[BENCHMARK], source=revision, directory=test_directory, declared_benchmarks=manifest)
                row = {"side": side, "source": revision, "status": "INCOMPLETE", "native": tested}
                records["samples"].append(row)
                if tested["status"] != "PASS" or tested["native_exit"] != 0:
                    raise RuntimeError(side + " native many-connection pilot failed")
                match tested["executed"]:
                    case [attempt] if not attempt["cached_locally"] and not attempt["execution_info"].get("cachedRemotely", False):
                        pass
                    case _:
                        raise ValueError("Pilot must have one uncached native test attempt")
                _, reports, errors = report.retained_reports(report.manifest_path(test_directory / "events.jsonl"))
                if errors:
                    raise ValueError("Cannot authenticate pilot reports: " + str(errors))
                [log] = [path for (owner, name), path in reports.items() if owner.owner.label == BENCHMARK and name == "test.log"]
                [configured] = tested["expected"]
                [actual_binary] = report.advertised_build_outputs(test_directory / "events.jsonl", {
                    (report.Owner(configured["label"], configured["configuration"]), "default")})
                if any(actual_binary[key] != records["builds"][side]["benchmark"][key] for key in ("sha256", "bytes", "name")):
                    raise ValueError("Timed benchmark differs from its completed premeasurement build")
                row["benchmark_output"] = actual_binary
                row["observations"] = observations(log.read_text(), iterations)
                row["runtime"] = prepared
                row["status"] = "PASS"
            if json.loads((directory / "runtime.json").read_text())["cleanup"] != "PASS":
                raise ValueError("Runtime configuration cleanup failed")
            if sorted(command("containers-after-" + side, ["docker", "ps", "-aq", "--no-trunc"]).split()) != records["containers_before"]:
                raise ValueError("Pilot left containers after " + side)
        records["status"] = "PASS"
    finally:
        completed = records["status"]
        records["status"] = "FAIL"
        cleanup_commands = [["git", "switch", "--detach", source]]
        if tag_changed:
            cleanup_commands.append(["docker", "tag", original_tag, tag] if original_tag else ["docker", "image", "rm", tag])
        for index, arguments in enumerate(cleanup_commands):
            try:
                command("cleanup-" + str(index), arguments, cleanup=True)
            except (OSError, subprocess.SubprocessError, TimeoutError) as error:
                records["cleanup_errors"].append(str(error))
        try:
            check_source(source, cleanup=True)
            if "containers_before" in records:
                records["containers_after"] = sorted(command("containers-after", ["docker", "ps", "-aq", "--no-trunc"], cleanup=True).split())
                if records["containers_after"] != records["containers_before"]:
                    raise ValueError("Unknown residual containers remain; no broad deletion attempted")
            if records["cleanup_errors"]:
                raise RuntimeError("Pilot cleanup failed: " + str(records["cleanup_errors"]))
            records.update(status=completed, cleanup="PASS")
        finally:
            report.write_json(evidence / "netip-many-pilot.json", records)
