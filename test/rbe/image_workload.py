"""Collect separate CPU profiles for the six qualified parallel iperf cases."""
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import tarfile
import time
import urllib.request
import uuid

import qualification_report as report
from image_runtime import prepared_runtime


BENCHMARK = "//test/benchmarks/network:iperf_test"
RELEASE = "//:release"
READER = "@com_github_google_pprof//:pprof"
NAMES = {f"BenchmarkIperfManyConnections/operation.{direction}/parallel.{parallel}": parallel
         for direction in ("Upload", "Download") for parallel in (4, 16, 64)}


def build_ids(path: Path) -> dict:
    """Read the actual ELF notes; Go profiles may omit a GNU build ID."""
    data = path.read_bytes()
    if data[:6] != b"\x7fELF\x02\x01":
        raise ValueError("Expected a little-endian ELF64 sentry")
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", data)
    notes = {"gnu": set(), "go": set()}
    for index in range(header[10]):
        segment = struct.unpack_from("<IIQQQQQQ", data, header[5] + index * header[9])
        if segment[0] != 4:  # PT_NOTE
            continue
        offset, end = segment[2], segment[2] + segment[5]
        while offset < end:
            namesz, descsz, kind = struct.unpack_from("<III", data, offset)
            offset += 12
            name = data[offset:offset + namesz].rstrip(b"\0")
            offset += (namesz + 3) & ~3
            description = data[offset:offset + descsz]
            offset += (descsz + 3) & ~3
            if offset > end or len(description) != descsz:
                raise ValueError("Truncated ELF note")
            if (name, kind) == (b"GNU", 3):
                notes["gnu"].add(description.hex())
            elif (name, kind) == (b"Go", 4):
                notes["go"].add(description.decode("ascii"))
    if any(len(values) > 1 for values in notes.values()) or not any(notes.values()):
        raise ValueError("Missing or ambiguous sentry build IDs")
    return {name: next(iter(values), None) for name, values in notes.items()}


def profile_summary(text: str, binary: Path, identifiers: dict) -> dict:
    samples = text.split("Samples:", 1)[1].split("Locations", 1)[0]
    values = [tuple(map(int, match)) for match in re.findall(r"^\s+(\d+)\s+(\d+):", samples, re.MULTILINE)]
    if ("samples/count cpu/nanoseconds" not in " ".join(samples.split()) or not values or
            sum(value[0] for value in values) <= 0 or sum(value[1] for value in values) <= 0):
        raise ValueError("CPU profile omitted positive native samples")
    mappings = text.split("\nMappings\n", 1)[1].splitlines()
    sentry, = [line for line in mappings if "/gvisor_sentry " in line]
    # The runtime records GNU IDs, not Go IDs. An absent GNU ID is explicitly
    # retained; the installed-file digest and source/output joins still apply.
    suffix = sentry.split("/gvisor_sentry ", 1)[1].strip()
    profile_id = suffix.split()[0] if suffix and not suffix.startswith("[") else None
    if profile_id is not None and profile_id != identifiers["gnu"]:
        raise ValueError("Profile GNU build ID differs from its actual sentry")
    return {"sample_count": sum(value[0] for value in values),
            "cpu_nanoseconds": sum(value[1] for value in values),
            "profile_mapping": sentry, "profile_gnu_build_id": profile_id,
            "binary": {"path": str(binary), **report.file_record(binary), "build_ids": identifiers}}


def observations(rows: list[dict], duration: int, omit: int) -> list[dict]:
    if Counter((row["Name"], row["Iterations"]) for row in rows) != Counter((name, 1) for name in NAMES):
        raise ValueError("Pilot omitted or duplicated a timed iperf3 observation")
    for row in rows:
        parallel = NAMES[row["Name"]]
        data = row["Output"]
        start, end = data["start"], data["end"]
        settings, summary = start["test_start"], end["sum_sent"]
        connected = [stream["socket"] for stream in start["connected"]]
        sent = [stream["sender"] for stream in end["streams"]]
        if (row["Parallel"] != parallel or data.get("error")
                or row["DurationSeconds"] != duration or row["OmitSeconds"] != omit
                or settings["duration"] != duration or settings["omit"] != omit
                or settings["num_streams"] != parallel or settings["bytes"] != 0 or settings["blocks"] != 0
                or settings["blksize"] != 131072 or settings["protocol"] != "TCP"
                or len(connected) != parallel or len(set(connected)) != parallel
                or len(sent) != parallel or {item["socket"] for item in sent} != set(connected)):
            raise ValueError("Actual iperf3 stream/time-mode identity differs")
        if summary["bytes"] != sum(item["bytes"] for item in sent) or summary["bytes"] <= 0:
            raise ValueError("iperf3 sender summary is not the complete positive aggregate")
        if any(not math.isfinite(summary[key]) or summary[key] <= 0 for key in ("seconds", "bits_per_second")):
            raise ValueError("iperf3 omitted finite positive transfer timing")
        row["zero_byte_streams"] = [item["socket"] for item in sent if item["bytes"] <= 0]
        if row["zero_byte_streams"]:
            raise ValueError("Timed pilot did not send data on every requested stream")
    return rows


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    source = config["source"]
    workspace = Path.cwd()
    tag = "gvisor.dev/images/benchmarks/iperf:latest"
    options = ["--config=x86_64", "--compilation_mode=opt", "--lockfile_mode=error",
               "--extra_toolchains=@llvm//toolchain:all", "--remote_download_outputs=all", "--noannounce_rc"]
    records = {"source": source, "status": "INCOMPLETE", "builds": {}, "samples": [], "decodes": [],
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

    def fetch(item: dict, destination: Path) -> None:
        request = urllib.request.Request("https://app.buildbuddy.io/api/v1/GetFile",
            data=json.dumps({"uri": item["uri"]}).encode(),
            headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(item["bytes"] + 1)
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("Retained iperf3 supplier identity mismatch")
        destination.write_bytes(data)

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
        bundle = evidence / "iperf3-supplier.tar.gz"
        fetch(supplier["bundle"], bundle)
        with tarfile.open(bundle) as archive:
            entries = [entry for entry in archive if entry.isfile()]
            if len({entry.name for entry in entries}) != len(entries):
                raise ValueError("Duplicate supplier evidence member")
            members = {entry.name: archive.extractfile(entry).read() for entry in entries}
        built = json.loads(members[supplier["phase"] + "/verdict.json"])
        if built["status"] != "PASS" or built["native_exit"] != 0 or built["source"] != supplier["source"]:
            raise ValueError("Retained iperf3 build is not source-bound PASS")
        events = evidence / "iperf3-supplier-events.jsonl"
        events.write_bytes(members[supplier["phase"] + "/events.jsonl"])
        [owner] = [report.Owner(row["label"], row["configuration"]) for row in built["expected"]
                   if row["label"] == "//images/benchmarks:iperf_archive"]
        [archive_output] = report.advertised_build_outputs(events, {(owner, "default")})
        if any(archive_output[key] != supplier["archive"][key] for key in ("name", "sha256", "bytes")):
            raise ValueError("Selected iperf3 archive differs from its configured output")
        archive_path = private / "iperf3.tar"
        fetch(supplier["archive"], archive_path)
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
            directory = evidence / ("invocation-" + side + "-build")
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
            records["builds"][side]["sentry_build_ids"] = build_ids(private / side / "gvisor-bin/gvisor_sentry")
            report.write_json(directory / "copied-outputs.json", outputs)

        iterations = config["iterations"]
        if iterations != 1:
            raise ValueError("Timed pilot requires exactly one transfer per case")
        manifest = evidence / "iperf-declared-benchmarks.json"
        report.write_json(manifest, {"owners": {BENCHMARK: [
            {"name": name, "count": 1, "iterations": iterations,
             "units": ["ns/op", "bandwidth.bytes_per_second"]} for name in sorted(NAMES)]}})
        for side, revision in (("before", config["before_source"]), ("after", source)):
            command("switch-" + side, ["git", "switch", "--detach", revision])
            check_source(revision)
            directory = evidence / side
            directory.mkdir()
            runtime = "qualification-netip-" + side + "-" + uuid.uuid4().hex
            configuration = {"runtime_name": runtime, "runtime_flags": ["--platform=systrap", "--network=sandbox", "--profile=true"],
                "image_name": "benchmarks/iperf", "lifecycle_only": True, "cleanup_reserve_seconds": 90,
                "retained_release_files": records["builds"][side]["runtime_files"],
                "release_supplier": {"source": revision, "phase": side + "-build"}}
            profiles = directory / "profiles"
            profiles.mkdir()
            with prepared_runtime(directory / "runtime.json", private / side, configuration) as prepared:
                if inspect()["Id"] != supplier["config_id"]:
                    raise ValueError("Pilot image changed between arms")
                selected = [*options, "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                    "--test_sharding_strategy=disabled", "--nozip_undeclared_test_outputs", "--test_timeout=600",
                    "--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=0", "--test_arg=-test.v=false",
                    "--test_arg=-test.run=^$", "--test_arg=-test.bench=^BenchmarkIperfManyConnections$",
                    "--test_arg=-test.benchtime=" + str(iterations) + "x", "--test_arg=-test.count=1",
                    "--test_arg=-runtime=" + runtime, "--test_arg=-pprof-cpu=true",
                    "--test_arg=-pprof-dir=" + str(profiles)]
                analysis_directory = evidence / ("invocation-" + side + "-pilot-analysis")
                test_directory = evidence / ("invocation-" + side + "-pilot")
                analysis = invoke(side + "-pilot-analysis", ["test", *selected, "--nobuild", BENCHMARK], "analysis",
                                  targets=[BENCHMARK], source=revision, directory=analysis_directory)
                if analysis["status"] != "PASS":
                    raise RuntimeError(side + " pilot analysis failed")
                tested = invoke(side + "-pilot", ["test", *selected, BENCHMARK], "test",
                    (analysis_directory / "events.jsonl",),
                    targets=[BENCHMARK], source=revision, directory=test_directory, declared_benchmarks=manifest, retain_outputs=True)
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
                raw = [json.loads(path.read_text()) for (owner, name), path in sorted(reports.items())
                       if owner.owner.label == BENCHMARK and name.startswith("test.outputs/iperf-") and name.endswith(".json")]
                [configured] = tested["expected"]
                [actual_binary] = report.advertised_build_outputs(test_directory / "events.jsonl", {
                    (report.Owner(configured["label"], configured["configuration"]), "default")})
                if any(actual_binary[key] != records["builds"][side]["benchmark"][key] for key in ("sha256", "bytes", "name")):
                    raise ValueError("Timed benchmark differs from its completed premeasurement build")
                row["benchmark_output"] = actual_binary
                row["observations"] = observations(raw, config["duration_seconds"], config["omit_seconds"])
                row["runtime"] = prepared
                expected_profiles = {profiles / runtime / name / "benchmarks/iperf/cpu.pprof" for name in NAMES}
                if set(profiles.rglob("*.pprof")) != expected_profiles:
                    raise ValueError("CPU profiles do not match all six actual sandboxed benchmark cases")
                for path in expected_profiles:
                    path.resolve(strict=True).relative_to(profiles.resolve())
                    if not path.is_file() or path.stat().st_size == 0:
                        raise ValueError("Missing or empty CPU profile: " + str(path))
                row["profiles"] = [{"name": str(path.relative_to(profiles / runtime).parent.parent.parent),
                    "path": str(path.relative_to(evidence)), **report.file_record(path)} for path in sorted(expected_profiles)]
                row["status"] = "PASS"
            if json.loads((directory / "runtime.json").read_text())["cleanup"] != "PASS":
                raise ValueError("Runtime configuration cleanup failed")
            if sorted(command("containers-after-" + side, ["docker", "ps", "-aq", "--no-trunc"]).split()) != records["containers_before"]:
                raise ValueError("Pilot left containers after " + side)
        # Both actual workloads and runtime cleanups finish before any decode.
        # A report failure cannot prevent the second arm from being captured.
        command("switch-reader", ["git", "switch", "--detach", source])
        check_source(source)
        built = invoke("profile-reader-build", ["build", *options, "--nostamp", READER], "build", targets=[READER])
        if built["status"] != "PASS" or built["native_exit"] != 0:
            raise RuntimeError("Declared standalone profile reader build failed")
        owner, = [report.Owner(item["label"], item["configuration"]) for item in built["expected"]]
        reader, = report.advertised_build_outputs(evidence / "invocation-profile-reader-build/events.jsonl", {(owner, "default")})
        executable = materialized(reader)
        records["reader"] = reader

        batches = [(row["side"], row["profiles"], private / row["side"] / "gvisor-bin/gvisor_sentry",
                    records["builds"][row["side"]]["sentry_build_ids"]) for row in records["samples"]]
        historical = config["historical_profiles"]
        old_bundle = evidence / "historical-profile-supplier.tar.gz"
        fetch(historical["artifact"], old_bundle)
        members = report.bundle_members(old_bundle, set(historical["members"]), verify=historical["members"], deadline=deadline)
        old_build = json.loads(members["source-builds/before/verdict.json"])
        old_test = json.loads(members["source-builds/before/pilot/verdict.json"])
        if any(item["source"] != historical["source"] or item["native_exit"] != 0 or item["status"] != "PASS"
               for item in (old_build, old_test)):
            raise ValueError("Historical before build/capture was not source-bound native PASS")
        old_events = evidence / "historical-build-events.jsonl"
        old_events.write_bytes(members["source-builds/before/events.jsonl"])
        old_owner, = [report.Owner(item["label"], item["configuration"]) for item in old_build["expected"] if item["label"] == RELEASE]
        old_outputs = report.advertised_build_outputs(old_events, {(old_owner, "default")})
        old_sentry, = [item for item in old_outputs if item["name"].endswith("/gvisor-bin/gvisor_sentry")]
        if any(old_sentry[key] != historical["sentry"][key] for key in ("name", "bytes", "sha256")):
            raise ValueError("Historical sentry differs from its configured output")
        old_binary = private / "historical/gvisor_sentry"
        old_binary.parent.mkdir()
        fetch(historical["sentry"], old_binary)
        old_ids = build_ids(old_binary)
        old_profiles = []
        for name, member in sorted(historical["profiles"].items()):
            path = evidence / "historical-profiles" / name / "cpu.pprof"
            path.parent.mkdir(parents=True)
            path.write_bytes(members[member])
            old_profiles.append({"name": name, "path": str(path.relative_to(evidence)), **report.file_record(path)})
        if {item["name"] for item in old_profiles} != set(NAMES):
            raise ValueError("Historical profile census differs")
        records["historical"] = {"supplier": historical, "profiles": old_profiles,
                                 "sentry_output": old_sentry, "sentry_build_ids": old_ids,
                                 "scope": "Before-only attribution; excluded from the new matched pair"}
        batches.append(("historical-before", old_profiles, old_binary, old_ids))
        wrapper = shlex.join(["timeout", "--signal=TERM", "--kill-after=5s", "45s", "bash", "-c",
            'set -eu; reader="$1"; binary="$2"; profile="$3"; expected="$4"; '
            'test "$(sha256sum "$reader" | cut -d " " -f1)" = "$expected"; '
            'timeout --signal=TERM --kill-after=5s 20s "$reader" -symbolize=none -raw "$binary" "$profile" > "$profile.raw"; '
            'timeout --signal=TERM --kill-after=5s 20s "$reader" -symbolize=none -top -nodecount=80 "$binary" "$profile" > "$profile.top"', "--"])
        errors = []
        for side, profiles, binary, identifiers in batches:
            for index, item in enumerate(profiles):
                path = evidence / item["path"]
                phase = f"{side}-profile-{index}"
                decoded = invoke(phase, ["run", *options, "--nostamp", "--run_under=" + wrapper, READER, "--",
                    str(binary), str(path), reader["sha256"]], "command", targets=[])
                row = {"side": side, "profile": item, "phase": phase, "native": decoded, "status": "FAIL"}
                records["decodes"].append(row)
                if decoded["status"] != "PASS" or decoded["native_exit"] != 0:
                    errors.append(phase + ": native reader failed")
                    continue
                try:
                    row.update(profile_summary(Path(str(path) + ".raw").read_text(), binary, identifiers))
                    row["reports"] = {suffix: report.file_record(Path(str(path) + suffix)) for suffix in (".raw", ".top")}
                    row["status"] = "PASS"
                except (OSError, ValueError) as error:
                    row["error"] = str(error)
                    errors.append(phase + ": " + str(error))
        records["decode_errors"] = errors
        if errors:
            raise RuntimeError("Profile reports incomplete: " + "; ".join(errors))
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
            report.write_json(evidence / "netip-many-profile.json", records)
