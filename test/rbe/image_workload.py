"""Qualify shared scratch utilities through their ordinary image targets."""
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
            # Each product image_index owns its musl transition. Ordinary Make
            # and these builds use the same CPU config, without a libc override.
            selected = [*options, "--config=" + architecture]
            targets = config["image_targets"][architecture]
            execution_log = private / (architecture + "-busybox-execution.json")
            try:
                built = invoke("busybox-" + architecture,
                               ["build", *selected, "--keep_going",
                                "--execution_log_json_file=" + str(execution_log),
                                "--noexecution_log_sort", *targets], "build", targets=targets)
            finally:
                record_execution(execution_log, architecture, "busybox")
            row = {"architecture": architecture, "build_status": built["status"], "outputs": {}}
            records["architectures"].append(row)
            queries = {
                "binary": ('filter("^(@@)?//images:busybox_binary$", deps(//images/basic:pidfd_tests_archive))', []),
                "config": ('filter("^(@@)?//images:busybox_build$", deps(//images/basic:pidfd_tests_archive))', ["--output_groups=busybox.config"]),
                "logs": ('filter("^(@@)?//images:busybox_build$", deps(//images/basic:pidfd_tests_archive))', ["--output_groups=Make_logs"]),
                "tools": ('filter("^(@@)?//images:busybox_(hostcc|nm)$", deps(//images/basic:pidfd_tests_archive))', []),
            }
            # Foreign_cc's Make log is an output of the same producing action;
            # its two scripts are action inputs. download_outputs=all retains
            # them without building a differently configured standalone target.
            for name, (query, extra) in queries.items():
                phase = architecture + "-busybox-" + name
                queried = invoke(phase, ["cquery", *selected, *extra, "--output=files", query],
                                 "command", targets=[])
                if queried["status"] != "PASS":
                    failed = True
                    continue
                paths = (evidence / ("invocation-" + phase) / "stdout.log").read_text().splitlines()
                if len(paths) != {"binary": 1, "config": 1, "logs": 3, "tools": 2}[name]:
                    raise ValueError("Unexpected configured BusyBox output count: " + phase)
                retained = []
                for index, output in enumerate(paths):
                    path = Path(output)
                    if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("bazel-out",):
                        raise ValueError(f"Unexpected configured output: {path}")
                    actual = execution_root / path
                    if not actual.is_file():
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
            raise RuntimeError("An image build or configured output query failed")
        records["status"] = "PASS"
        (evidence / "busybox-build-inputs.json").write_text(json.dumps(records, indent=2) + "\n")
        scripts = Path(__file__).parent
        guards = invoke("leaf-archive-guards", ["run", "--lockfile_mode=error", "--noannounce_rc",
                        "@rules_python//python/bin:python", "--",
                        str(scripts / "image_archive_test.py")], "command", targets=[])
        if guards["status"] != "PASS":
            raise RuntimeError("Scratch archive reporting guards failed")
        inspected = invoke("leaf-image-closure", ["run", "--lockfile_mode=error", "--noannounce_rc",
                           "@rules_python//python/bin:python", "--",
                           str(scripts / "leaf_image_inspect.py"), str(evidence)], "command", targets=[])
        if inspected["status"] != "PASS":
            raise RuntimeError("Scratch leaf image closure verification failed")
        import image_pidfd_runtime
        image_pidfd_runtime.run(invoke, evidence, private)
        import image_leaf_runtime
        image_leaf_runtime.run(invoke, evidence, private)
    finally:
        (evidence / "busybox-build-inputs.json").write_text(json.dumps(records, indent=2) + "\n")
