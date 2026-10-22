"""Build the scratch PIDFD image and qualify its existing runtime consumer."""
import hashlib
import json
import os
from pathlib import Path
import shutil


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    profile_targets = list(dict.fromkeys(row["owner"] for row in config["profile_helper_outputs"]))
    profile_mode = "--compilation_mode=" + config["profile_helper_compilation_mode"]
    if set(config["image_targets"]).intersection(profile_targets) or set(config["targets"]) != set(config["image_targets"] + profile_targets):
        raise ValueError("Image and profile-helper targets must partition the selected build")
    options = ["--compilation_mode=opt", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc",
               "--build_event_json_file_path_conversion=false"]
    root_name = "canonical-execution-root"
    result = invoke(root_name, ["info", *options, "execution_root"], "command", targets=[])
    if result["status"] != "PASS":
        raise RuntimeError("Cannot identify canonical execution root")
    execution_root = Path((evidence / ("invocation-" + root_name) / "stdout.log").read_text().strip())
    if not execution_root.is_absolute() or not execution_root.is_dir():
        raise ValueError("Invalid canonical execution root")
    records = {"source": config["source"], "status": "INCOMPLETE", "architectures": []}
    failed = False
    try:
        for architecture in ("x86_64", "aarch64"):
            selected = [*options, "--config=" + architecture,
                        "--platforms=" + config["image_platforms"][architecture]]
            execution_log = evidence / (architecture + "-busybox-execution.json")
            built = invoke("busybox-" + architecture,
                           ["build", *selected, "--keep_going", "--output_groups=+Make_logs",
                            "--execution_log_json_file=" + str(execution_log),
                            "--noexecution_log_sort", *config["image_targets"]],
                           "build", targets=config["image_targets"])
            # Retain build failures and continue the independent architecture.
            row = {"architecture": architecture, "build_status": built["status"], "outputs": {}}
            records["architectures"].append(row)
            if execution_log.is_file():
                try:
                    record_execution(execution_log, architecture, "busybox")
                except (OSError, ValueError, RuntimeError) as error:
                    # The shared recorder already preserves the overall failure.
                    row["execution_evidence_error"] = str(error)
            queries = {
                "binary": ("//images:busybox_binary", []),
                "config": ("//images:busybox_config", []),
                "logs": ("//images:busybox_build", ["--output_groups=Make_logs"]),
                "tools": ('filter("^//images:busybox_(hostcc|nm)$", deps(//images:busybox_build))', []),
            }
            for name, (query, extra) in queries.items():
                phase = architecture + "-busybox-" + name
                queried = invoke(phase, ["cquery", *selected, *extra, "--output=files", query],
                                 "command", targets=[])
                if queried["status"] != "PASS":
                    failed = True
                    continue
                paths = (evidence / ("invocation-" + phase) / "stdout.log").read_text().splitlines()
                retained = []
                for index, output in enumerate(paths):
                    path = Path(output)
                    if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("bazel-out",):
                        raise ValueError(f"Unexpected configured output: {path}")
                    actual = execution_root / path
                    if not actual.is_file():
                        # Failed foreign builds may omit outputs; preserve available diagnostics.
                        retained.append({"output": output, "missing": True})
                        continue
                    destination = evidence / (phase + "-" + str(index) + "-" + path.name)
                    if name != "binary":
                        if actual.stat().st_size > 8 * 1024 * 1024:
                            raise ValueError("BusyBox diagnostic output exceeds 8 MiB")
                        shutil.copyfile(actual, destination)
                    retained.append({"output": str(actual), "retained": str(destination) if name != "binary" else None})
                row["outputs"][name] = retained
            failed |= built["status"] != "PASS"
        for architecture in ("x86_64", "aarch64"):
            execution_log = evidence / (architecture + "-profile-helper-execution.json")
            built = invoke("profile-helper-" + architecture,
                           ["build", *options, "--config=" + architecture, profile_mode,
                            "--keep_going", "--execution_log_json_file=" + str(execution_log),
                            "--noexecution_log_sort", *profile_targets], "build", targets=profile_targets)
            profile = {"architecture": architecture, "build_status": built["status"]}
            records.setdefault("profile_builds", []).append(profile)
            if execution_log.is_file():
                try:
                    record_execution(execution_log, architecture, "profile-helper")
                except (OSError, ValueError, RuntimeError) as error:
                    profile["execution_evidence_error"] = str(error)
            failed |= built["status"] != "PASS"
        if failed:
            raise RuntimeError("An image build or configured output query failed")
        for architecture in ("x86_64", "aarch64"):
            expected = [row for row in config["profile_helper_outputs"]
                        if row["architecture"] == architecture]
            comparisons = []
            destination = evidence / (architecture + "-profile-helper-equality.json")
            try:
                for index, row in enumerate(expected):
                    name = f"profile-output-{architecture}-{index}"
                    result = invoke(name, ["cquery", *options, "--config=" + architecture, profile_mode,
                                          "--output=files", row["owner"]],
                                    "command", targets=[])
                    if result["status"] != "PASS":
                        raise RuntimeError("Cannot locate profile-helper output")
                    lines = (evidence / ("invocation-" + name) / "stdout.log").read_text().splitlines()
                    match lines:
                        case [output]:
                            path = Path(output)
                            if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("bazel-out",):
                                raise ValueError(f"Unexpected image output path: {path}")
                            payload = (execution_root / path).read_bytes()
                        case _:
                            raise ValueError(f"Expected one output for {row['owner']}: {lines!r}")
                    actual = {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
                    wanted = {key: row[key] for key in ("sha256", "bytes")}
                    comparisons.append({"owner": row["owner"], "actual": actual,
                                        "expected": wanted, "equal": actual == wanted})
                    if actual != wanted:
                        raise ValueError(f"Profile-helper output changed: {row['owner']}")
            finally:
                destination.write_text(json.dumps(comparisons, indent=2) + "\n")
        records["status"] = "PASS"
        (evidence / "busybox-build-inputs.json").write_text(json.dumps(records, indent=2) + "\n")
        import image_pidfd_runtime
        image_pidfd_runtime.run(invoke, evidence, private)
    finally:
        (evidence / "busybox-build-inputs.json").write_text(json.dumps(records, indent=2) + "\n")
