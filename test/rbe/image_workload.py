"""Build the three source-bound allocator binaries and run one paired cohort."""
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


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    source = config["source"]
    workspace = Path.cwd()
    package = workspace / "test/rbe/state_array_pair"
    benchmark = "//pkg/sentry/pgalloc:pgalloc_test"
    options = ["--config=x86_64", "--compilation_mode=opt", "--lockfile_mode=error",
               "--remote_download_outputs=toplevel", "--noannounce_rc"]
    record = {"source": source, "status": "INCOMPLETE", "builds": [], "cleanup": "INCOMPLETE"}
    inputs = {"binaries": {}, "benchstat": config["benchstat"]}

    def fetch(item: dict) -> bytes:
        request = urllib.request.Request(
            "https://app.buildbuddy.io/api/v1/GetFile", data=json.dumps({"uri": item["uri"]}).encode(),
            headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(item["bytes"] + 1)
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("Retained CAS artifact identity mismatch")
        return data

    def materialized(root: Path, item: dict, allowed_root: Path) -> Path:
        relative = Path(item["name"])
        if relative.is_absolute() or relative.parts[:1] != ("bazel-out",) or ".." in relative.parts:
            raise ValueError("Unexpected configured allocator output path")
        binary = (root / relative).resolve(strict=True)
        binary.relative_to(allowed_root.resolve())
        if report.file_record(binary) != {key: item[key] for key in ("bytes", "sha256")} or not os.access(binary, os.X_OK):
            raise ValueError("Allocator executable differs from its default BEP output")
        return binary

    def command(argv: list[str]) -> str:
        remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Allocator benchmark work deadline exhausted")
        return subprocess.check_output(argv, text=True, timeout=min(60, remaining)).strip()

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

    try:
        check_source(source)
        host_probe = json.loads((Path(os.environ["BUILDBUDDY_ARTIFACTS_DIRECTORY"]) / "pgalloc-worker.json").read_text())
        if not host_probe["selectedProcessorFeatures"]["erms"]:
            raise ValueError("Measurement worker does not expose ERMS")
        report.write_json(evidence / "host-preflight.json", host_probe)
        # Reuse the successful corrected build, preserving the failed enclosing
        # qualification. The exact source/owner/output comes from its real BEP.
        supplier = config["corrected_supplier"]
        supplier_path = evidence / "retained-corrected-qualification.tar.gz"
        supplier_path.write_bytes(fetch(supplier["artifact"]))
        directory = evidence / "source-builds/corrected"
        directory.mkdir(parents=True)
        with tarfile.open(supplier_path) as archive:
            selected = [member for member in archive if member.isfile() and member.name.startswith("invocation-corrected-build/")]
            names = [member.name for member in selected]
            if len(names) != len(set(names)):
                raise ValueError("Duplicate retained build evidence")
            for member in selected:
                relative = Path(member.name).relative_to("invocation-corrected-build")
                if ".." in relative.parts:
                    raise ValueError("Unsafe retained build path")
                destination = directory / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.extractfile(member).read())
        result = json.loads((directory / "verdict.json").read_text())
        if result["source"] != supplier["source"] or result["status"] != "PASS" or result["native_exit"] != 0:
            raise ValueError("Retained corrected build did not pass at its exact source")
        [expected] = result["expected"]
        owner = report.Owner(expected["label"], expected["configuration"])
        if owner.label != benchmark:
            raise ValueError("Unexpected retained allocator owner")
        [retained] = report.advertised_build_outputs(directory / "events.jsonl", {(owner, "default")})
        if retained != supplier["output"]:
            raise ValueError("Retained default output differs from the selected supplier")
        retained_root = private / "retained-corrected"
        retained_binary = retained_root / retained["name"]
        retained_binary.parent.mkdir(parents=True, exist_ok=True)
        retained_binary.write_bytes(fetch(retained))
        retained_binary.chmod(0o755)
        materialized(retained_root, retained, retained_root)
        controls = {"actual_bytestream_output": "PASS"}
        for name, item in (("wrong_digest", {**retained, "sha256": "0" * 64}),
                           ("missing_output", {**retained, "name": retained["name"] + ".missing"})):
            try:
                materialized(retained_root, item, retained_root)
            except (OSError, ValueError):
                controls[name] = "PASS"
            else:
                raise ValueError("Resolver accepted " + name)
        report.write_json(evidence / "materialization-controls.json", controls)
        record["corrected_reuse"] = supplier
        for side, revision in (("corrected", supplier["source"]), ("reviewed", config["reviewed_source"]),
                               ("before", config["before_source"])):
            directory = evidence / ("source-builds/" + side)
            if side != "corrected":
                command(["git", "switch", "--detach", revision])
                check_source(revision)
                result = invoke(side + "-build", ["build", *options, benchmark], "build",
                                targets=[benchmark], source=revision, directory=directory)
                if result["status"] != "PASS" or result["native_exit"] != 0:
                    raise RuntimeError(side + " allocator build failed")
            match result["expected"]:
                case [{"label": label, "configuration": configuration}] if label == benchmark:
                    owner = report.Owner(label, configuration)
                case _:
                    raise ValueError("Unexpected configured allocator owner")
            outputs = report.advertised_build_outputs(directory / "events.jsonl", {(owner, "default")})
            match outputs:
                case [item] if report.Owner.parse(item["identity"]) == owner:
                    pass
                case _:
                    raise ValueError("Expected one configured allocator executable")
            binary = materialized(retained_root, item, retained_root) if side == "corrected" else materialized(workspace, item, output_root)
            identity = {key: item[key] for key in ("bytes", "sha256")}
            relative = f"arms/{side}/_main/pkg/sentry/pgalloc/pgalloc_test"
            staged = private / "paired-inputs" / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(binary, staged)
            if report.file_record(staged) != identity:
                raise ValueError("Copied allocator executable differs")
            inputs["binaries"][relative] = {
                "size": identity["bytes"], "sha256": identity["sha256"], "source": revision,
                "owner": item["identity"], "bep_name": item["name"],
            }
            report.write_json(directory / "copied-outputs.json", [item])
            record["builds"].append({"side": side, "source": revision,
                                    "verdict": str((directory / "verdict.json").relative_to(evidence)),
                                    "outputs": [item], "materialized": {"path": str(binary), **identity},
                                    "reused": side == "corrected"})
        command(["git", "switch", "--detach", source])
        check_source(source)
        shutil.copytree(private / "paired-inputs/arms", package / "arms")

        benchstat = config["benchstat"]
        data = fetch(benchstat)
        (package / "benchstat").write_bytes(data)
        (package / "benchstat").chmod(0o755)
        report.write_json(package / "inputs.json", inputs)
        report.write_json(evidence / "allocator-benchmark-inputs.json", inputs)
        selected = [*options, "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                    "--nozip_undeclared_test_outputs", "--test_sharding_strategy=disabled", "--test_timeout=1800"]
        targets = ["//test/rbe/state_array_pair:paired_test"]
        analysis = invoke("paired-analysis", ["test", *selected, "--nobuild", *targets], "analysis", targets=targets)
        if analysis["status"] != "PASS":
            raise RuntimeError("Allocator cohort analysis failed")
        result = invoke("paired", ["test", *selected, *targets], "test",
                        (evidence / "invocation-paired-analysis/events.jsonl",), targets=targets, retain_outputs=True)
        if result["status"] != "PASS" or result["native_exit"] != 0:
            raise RuntimeError("Allocator cohort failed")
        record["status"] = "PASS"
    finally:
        completed_status = "PASS" if record["status"] == "PASS" else "FAIL"
        record["status"] = "FAIL"
        try:
            subprocess.run(["git", "switch", "--detach", source], check=True, timeout=30)
            check_source(source)
            record["cleanup"] = "PASS"
            record["status"] = completed_status
        finally:
            report.write_json(evidence / "allocator-benchmark-builds.json", record)
