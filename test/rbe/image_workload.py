"""Stage matched state-test binaries and run the existing paired fixture."""
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
    owner = "//pkg/state/tests:tests_test"
    paired = "//test/rbe/state_array_pair:paired_test"
    if config["targets"] != [paired]:
        raise ValueError("Unexpected paired state owner")
    options = ["--config=x86_64", "--compilation_mode=opt", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc"]
    workspace = Path.cwd()
    package = workspace / "test/rbe/state_array_pair"
    staged = private / "paired-inputs"
    staged.mkdir()
    lock = workspace / "MODULE.bazel.lock"
    lock_identity = report.file_record(lock)
    records = {"status": "INCOMPLETE", "source": source, "before_source": config["before_source"],
               "builds": [], "inputs": {}, "lock": lock_identity}

    def remaining() -> float:
        value = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
        if value <= 0:
            raise TimeoutError("Paired state work deadline exhausted")
        return value

    def check_source(expected: str) -> dict:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, timeout=30).strip()
        if head != expected or subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], timeout=30):
            raise RuntimeError("Expected clean source " + expected)
        if report.file_record(lock) != lock_identity:
            raise ValueError("Matched build lock changed")
        inputs = {}
        for name, digest in config["matched_inputs"].items():
            path = workspace / name
            data = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("Matched source input changed: " + name)
            inputs[name] = {"bytes": len(data), "sha256": digest}
        if report.file_record(workspace / "pkg/state/codec_decode_unsafe.go")["sha256"] != config["decoder_sha256"][expected]:
            raise ValueError("Decoder input differs from selected arm")
        return inputs

    try:
        records["matched_inputs"] = check_source(source)
        delta = subprocess.check_output(["git", "diff", "--name-only", config["before_source"], source], text=True, timeout=30).splitlines()
        if delta != ["pkg/state/codec_decode_unsafe.go"]:
            raise ValueError("Paired sources differ outside the decoder: " + repr(delta))
        # Reuse the already-qualified benchstat executable, authenticating the
        # entire file on this worker before any measured child can start.
        supplier = config["benchstat"]
        request = urllib.request.Request("https://app.buildbuddy.io/api/v1/GetFile",
            data=json.dumps({"uri": supplier["uri"]}).encode(),
            headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        with urllib.request.urlopen(request, timeout=min(180, remaining())) as response:
            data = response.read(supplier["bytes"] + 1)
        if len(data) != supplier["bytes"] or hashlib.sha256(data).hexdigest() != supplier["sha256"]:
            raise ValueError("Qualified benchstat file identity differs")
        (staged / "benchstat").write_bytes(data)
        (staged / "benchstat").chmod(0o755)
        records["benchstat"] = supplier
        for side, revision, directory in (
            ("after", source, evidence / "invocation-after-build"),
            ("before", config["before_source"], evidence / "source-builds/before"),
        ):
            subprocess.run(["git", "switch", "--detach", revision], check=True, timeout=min(30, remaining()))
            check_source(revision)
            result = invoke(side + "-build", ["build", *options, owner], "build",
                            targets=[owner], source=revision, directory=directory)
            if result["status"] != "PASS" or result["native_exit"] != 0:
                raise RuntimeError(side + " source build failed")
            match result["expected"]:
                case [{"label": label, "configuration": configuration}] if label == owner:
                    configured_owner = report.Owner(label, configuration)
                case other:
                    raise ValueError("Unexpected configured state build owners: " + repr(other))
            outputs = report.advertised_build_outputs(directory / "events.jsonl", {(configured_owner, "default")})
            match outputs:
                case [item] if report.Owner.parse(item["identity"]) == configured_owner:
                    pass
                case other:
                    raise ValueError("Expected one configured default executable: " + repr(other))
            uri = urlsplit(item["uri"])
            if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
                raise ValueError("Expected the explicitly materialized local BEP output")
            original = Path(unquote(uri.path)).resolve(strict=True)
            original.relative_to(output_root.resolve(strict=True))
            identity = {key: item[key] for key in ("sha256", "bytes")}
            if report.file_record(original) != identity or not os.access(original, os.X_OK):
                raise ValueError("Materialized binary differs from its default BEP output")
            relative = f"arms/{side}/_main/pkg/state/tests/tests_test"
            destination = staged / relative
            destination.parent.mkdir(parents=True)
            shutil.copy2(original, destination)
            if report.file_record(destination) != identity:
                raise ValueError("Copied benchmark binary changed")
            records["inputs"][relative] = {"source": revision, "owner": item["identity"],
                                           "size": identity["bytes"], "sha256": identity["sha256"],
                                           "bep_name": item["name"]}
            records["builds"].append({"side": side, "source": revision,
                "verdict": {"path": str((directory / "verdict.json").relative_to(evidence)), **report.file_record(directory / "verdict.json")},
                "output": item})
        subprocess.run(["git", "switch", "--detach", source], check=True, timeout=min(30, remaining()))
        check_source(source)
        if any((package / name).exists() for name in ("arms", "benchstat", "inputs.json")):
            raise ValueError("Paired package already has staged inputs")
        shutil.copytree(staged / "arms", package / "arms")
        shutil.copy2(staged / "benchstat", package / "benchstat")
        report.write_json(package / "inputs.json", {"binaries": records["inputs"], "benchstat": supplier})
        report.write_json(evidence / "paired-inputs.json", records)
        selected = [*options, "--strategy=TestRunner=local", "--nocache_test_results",
                    "--runs_per_test=1", "--flaky_test_attempts=1", "--test_sharding_strategy=disabled",
                    "--test_output=all", "--nozip_undeclared_test_outputs", "--test_timeout=900"]
        analysis = invoke("paired-analysis", ["test", *selected, "--nobuild", paired], "analysis", targets=[paired])
        if analysis["status"] != "PASS":
            raise RuntimeError("Paired benchmark analysis failed")
        tested = invoke("paired", ["test", *selected, paired], "test",
                        expected_analysis=(evidence / "invocation-paired-analysis/events.jsonl",),
                        targets=[paired], retain_outputs=True)
        if tested["status"] != "PASS" or tested["native_exit"] != 0:
            raise RuntimeError("Paired benchmark cohort failed")
        records["status"] = "PASS"
    finally:
        completed_status = records["status"]
        records["status"] = "FAIL"
        try:
            subprocess.run(["git", "switch", "--detach", source], check=True, timeout=30)
            check_source(source)
            records["restored_source"] = source
            records["restored_lock"] = report.file_record(lock)
            records["status"] = completed_status
        finally:
            report.write_json(evidence / "state-array-paired.json", records)
