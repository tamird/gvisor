"""Build the three source-bound allocator binaries and run one paired cohort."""
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
        for side, revision in (("corrected", source), ("reviewed", config["reviewed_source"]),
                               ("before", config["before_source"])):
            command(["git", "switch", "--detach", revision])
            check_source(revision)
            directory = evidence / ("invocation-corrected-build" if side == "corrected" else "source-builds/" + side)
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
            uri = urlsplit(item["uri"])
            if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
                raise ValueError("Allocator executable was not materialized")
            binary = Path(unquote(uri.path)).resolve(strict=True)
            binary.relative_to(output_root.resolve())
            identity = {key: item[key] for key in ("bytes", "sha256")}
            if report.file_record(binary) != identity or not os.access(binary, os.X_OK):
                raise ValueError("Allocator executable differs from its default BEP output")
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
                                    "outputs": [item]})
        command(["git", "switch", "--detach", source])
        check_source(source)
        shutil.copytree(private / "paired-inputs/arms", package / "arms")

        benchstat = config["benchstat"]
        request = urllib.request.Request(
            "https://app.buildbuddy.io/api/v1/GetFile", data=json.dumps({"uri": benchstat["uri"]}).encode(),
            headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(benchstat["bytes"] + 1)
        if len(data) != benchstat["bytes"] or hashlib.sha256(data).hexdigest() != benchstat["sha256"]:
            raise ValueError("Retained source-built benchstat identity mismatch")
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
