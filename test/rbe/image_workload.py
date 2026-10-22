"""Build both BusyBox image closures, then probe the native shell contract."""
import json
import os
from pathlib import Path
import shutil


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
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
                            "--noexecution_log_sort", *config["targets"]],
                           "build", targets=config["targets"])
            # Retain build failures and continue the independent architecture.
            row = {"architecture": architecture, "build_status": built["status"], "outputs": {}}
            records["architectures"].append(row)
            if execution_log.is_file():
                try:
                    record_execution(execution_log, architecture, "busybox")
                except (OSError, ValueError, RuntimeError) as error:
                    row["execution_evidence_error"] = str(error)
                    failed = True
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
        if failed:
            raise RuntimeError("A BusyBox image build or configured output query failed")
        scripts = Path(__file__).parent
        python = ["run", "--lockfile_mode=error", "--noannounce_rc",
                  "@rules_python//python/bin:python", "--"]
        contents = evidence / "pidfd-contents.json"
        inspected = invoke("pidfd-image-closure", [*python, str(scripts / "pidfd_image_inspect.py"), str(contents)],
                           "command", targets=[])
        if inspected["status"] != "PASS":
            raise RuntimeError("Existing image/source/timeout closure inspection failed")
        manifest = evidence / "busybox-build-inputs.json"
        manifest.write_text(json.dumps(records, indent=2) + "\n")
        probed = invoke("busybox-shell-probes", [*python, str(scripts / "busybox_probe.py"),
                        str(manifest), str(contents), str(evidence / "busybox-probes.json")],
                        "command", targets=[])
        if probed["status"] != "PASS":
            raise RuntimeError("BusyBox static/configuration/shell probes failed")
        records["status"] = "PASS"
    finally:
        (evidence / "busybox-build-inputs.json").write_text(json.dumps(records, indent=2) + "\n")
