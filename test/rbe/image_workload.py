"""Check the compiler migration through its existing build and test owners."""

import json
import os
from pathlib import Path

import qualification_report as report


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
        guarded = invoke("execution-log-stream-guards",
                         ["run", *options, "@rules_python//python/bin:python", "--",
                          str(Path.cwd() / "test/rbe/image_execution_test.py")], "command", targets=[])
        if guarded["status"] != "PASS":
            raise RuntimeError("Execution-log streaming guards failed")
        for group in config["build_groups"]:
            row = {**group, "status": "INCOMPLETE"}
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
