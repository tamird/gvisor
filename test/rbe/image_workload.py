"""Check the compiler migration through its existing build and test owners."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile
import time
import urllib.request

import qualification_report as report


def retained_outputs(config, evidence: Path, private: Path) -> dict:
    """Materialize completed native outputs; do not rebuild or relabel them."""
    supplier = config["retained_builds"]

    def fetch(uri: str, expected: dict, destination: Path) -> None:
        request = urllib.request.Request(
            "https://app.buildbuddy.io/api/v1/GetFile",
            data=json.dumps({"uri": uri}).encode(),
            headers={"Content-Type": "application/json",
                     "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Retained compiler output materialization deadline exhausted")
        size = 0
        digest = hashlib.sha256()
        with urllib.request.urlopen(request, timeout=min(60, remaining)) as response, destination.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > expected["bytes"] or time.monotonic() >= float(os.environ["APPLICABILITY_WORK_DEADLINE"]):
                    raise ValueError("Retained compiler output exceeded its size or time bound")
                digest.update(chunk)
                output.write(chunk)
        if {"bytes": size, "sha256": digest.hexdigest()} != {key: expected[key] for key in ("bytes", "sha256")}:
            raise ValueError("Retained compiler output identity differs")

    artifact = private / "retained-compiler-qualification.tar.gz"
    fetch(supplier["artifact"]["uri"], supplier["artifact"], artifact)
    shutil.copyfile(artifact, evidence / artifact.name)
    restored = {}
    downloaded = {}
    with tarfile.open(artifact) as archive:
        def member(name: str) -> bytes:
            matches = [entry for entry in archive.getmembers() if entry.name == name]
            if len(matches) != 1 or not matches[0].isfile():
                raise ValueError("Expected one regular retained compiler member: " + name)
            return archive.extractfile(matches[0]).read()

        manifest = json.loads(member("compiler-current-build.json"))
        if manifest["source"] != supplier["source"]:
            raise ValueError("Retained compiler source differs")
        for name in supplier["groups"]:
            expected = next(group for group in config["build_groups"] if group["name"] == name)
            groups = [group for group in manifest["builds"] if group["name"] == name]
            if len(groups) != 1 or groups[0]["status"] != "PASS" or any(groups[0][key] != value for key, value in expected.items()):
                raise ValueError("Retained compiler group differs: " + name)
            group = groups[0]
            native = json.loads(member("invocation-" + name + "/verdict.json"))
            if native["source"] != supplier["source"] or native["status"] != "PASS" or native["native_exit"] != 0:
                raise ValueError("Retained compiler native verdict differs: " + name)
            events = evidence / ("retained-" + name + ".events.jsonl")
            events.write_bytes(member("invocation-" + name + "/events.jsonl"))
            owners = {(report.Owner(owner["label"], owner["configuration"]), "default") for owner in native["expected"]}
            outputs = report.advertised_build_outputs(events, owners)
            if outputs != [{key: value for key, value in output.items() if key != "materialized_path"} for output in group["outputs"]]:
                raise ValueError("Retained compiler outputs differ from native BEP: " + name)
            for output in group["outputs"]:
                identity = (output["sha256"], output["bytes"])
                if identity not in downloaded:
                    path = private / ("retained-output-" + output["sha256"])
                    uri = supplier["cas_uri_prefix"] + output["sha256"] + "/" + str(output["bytes"])
                    fetch(uri, output, path)
                    downloaded[identity] = path
                output["materialized_path"] = str(downloaded[identity])
            group["build_source"] = supplier["source"]
            group["build_invocation"] = supplier["invocation_id"]
            restored[name] = group
    report.write_json(evidence / "retained-compiler-builds.json", {
        "supplier": supplier, "groups": list(restored.values()),
        "scope": "Exact prior outputs materialized from CAS; no new compilation or execution proof."})
    return restored


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    options = ["--compilation_mode=opt", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc",
               "--build_event_json_file_path_conversion=false"]
    records = {"source": config["source"], "status": "INCOMPLETE", "builds": [], "tests": []}
    failed = False
    try:
        result = invoke("execution-root", ["info", *options, "execution_root"], "command", targets=[])
        if result["status"] != "PASS":
            raise RuntimeError("Cannot identify canonical execution root")
        root = Path((evidence / "invocation-execution-root/stdout.log").read_text().strip())
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("Invalid canonical execution root")
        completed = retained_outputs(config, evidence, private)
        for group in config["build_groups"]:
            if group["name"] in completed:
                records["builds"].append(completed[group["name"]])
                continue
            row = {**group, "build_source": config["source"], "status": "INCOMPLETE"}
            records["builds"].append(row)
            phase = group["name"]
            # Unprocessed or incomplete raw logs must not enter the compact
            # evidence scan or bundle if the recorder fails.
            execution = private / (phase + "-execution.json")
            built = invoke(phase, ["build", *options, *group["options"], "--keep_going",
                           "--execution_log_json_file=" + str(execution), "--noexecution_log_sort",
                           *group["targets"]], "build", targets=group["targets"])
            row["build_status"] = built["status"]
            if execution.is_file():
                try:
                    record_execution(execution, group["architecture"], phase)
                except (OSError, ValueError, RuntimeError) as error:
                    # The shared recorder retains runner/capture FAIL. Continue
                    # unrelated groups without granting execution proof here.
                    row["execution_error"] = str(error)
                    failed = True
            else:
                row["execution_error"] = "Native execution log missing"
                failed = True
            if built["status"] != "PASS":
                failed = True
                continue
            try:
                owners = {(report.Owner(owner["label"], owner["configuration"]), "default")
                          for owner in built["expected"]}
                outputs = report.advertised_build_outputs(evidence / ("invocation-" + phase) / "events.jsonl", owners)
                if len(outputs) != len(group["targets"]) or {out["identity"]["label"] for out in outputs} != set(group["targets"]):
                    raise ValueError("Expected one default executable or library per configured owner")
                row["outputs"] = outputs
                for output in outputs:
                    relative = Path(output["name"])
                    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("bazel-out",):
                        raise ValueError("Unexpected configured output: " + output["name"])
                    actual = report.file_record(root / relative)
                    if actual != {key: output[key] for key in ("sha256", "bytes")}:
                        raise ValueError("Materialized output differs from default BEP identity")
                    output["materialized_path"] = str(root / relative)
                row["status"] = "PASS"
            except (OSError, ValueError) as error:
                row["output_error"] = str(error)
                failed = True
        # Every successful independent build retains its outputs even if another
        # architecture failed. The inspector consumes only complete owner sets.
        manifest = evidence / "compiler-builds.json"
        report.write_json(manifest, records)
        inspected = invoke("compiler-output-inspection",
                           ["run", *options, "@rules_python//python/bin:python", "--",
                            str(Path.cwd() / "test/rbe/compiler_inspect.py"), str(manifest),
                            str(evidence / "compiler-outputs.json")], "command", targets=[])
        failed |= inspected["status"] != "PASS"
        for group in config["test_groups"]:
            result = invoke(group["name"], ["test", *options, *group["options"], "--keep_going",
                            "--nocache_test_results", "--test_output=errors", *group["targets"]],
                            "test", targets=group["targets"])
            records["tests"].append({**group, "status": result["status"]})
            failed |= result["status"] != "PASS"
        if failed:
            raise RuntimeError("One or more compiler build, output, or owning test gates failed")
        records["status"] = "PASS"
    finally:
        report.write_json(evidence / "compiler-current-build.json", records)
