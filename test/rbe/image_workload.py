"""Build GNU timeout for both musl targets and verify the selected static ELFs."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    targets = config["targets"]
    binary_target = "//images:timeout_binary"
    options = ["--compilation_mode=opt", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc",
               "--build_event_json_file_path_conversion=false"]
    records = {"source": config["source"], "status": "INCOMPLETE",
               "scope": "Both target builds and static ELF identity; no runtime or license audit claim",
               "architectures": []}
    result = invoke("execution-root", ["info", *options, "execution_root"], "command", targets=[])
    if result["status"] != "PASS":
        raise RuntimeError("Cannot identify canonical execution root")
    root = Path((evidence / "invocation-execution-root/stdout.log").read_text().strip())
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("Invalid canonical execution root")
    failed = False
    try:
        for architecture, machine in [("x86_64", "Advanced Micro Devices X86-64"), ("aarch64", "AArch64")]:
            selected = [*options, "--config=" + architecture,
                        "--platforms=" + config["image_platforms"][architecture]]
            phase = "timeout-" + architecture
            execution = evidence / (phase + "-execution.json")
            row = {"architecture": architecture, "status": "INCOMPLETE", "outputs": {}}
            records["architectures"].append(row)
            built = invoke(phase, ["build", *selected, "--output_groups=+Configure_logs",
                           "--execution_log_json_file=" + str(execution),
                           "--noexecution_log_sort", *targets], "build", targets=targets)
            row["build_status"] = built["status"]
            if execution.is_file():
                record_execution(execution, architecture, "timeout")
            paths = {}
            for name, query, groups in [("binary", "//images:timeout_binary", []),
                                        ("logs", "//images:timeout_build", ["--output_groups=Configure_logs"])]:
                query_phase = phase + "-" + name
                queried = invoke(query_phase, ["cquery", *selected, *groups, "--output=files", query], "command", targets=[])
                if queried["status"] != "PASS":
                    failed = True
                    continue
                paths[name] = []
                for index, value in enumerate((evidence / ("invocation-" + query_phase) / "stdout.log").read_text().splitlines()):
                    relative = Path(value)
                    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("bazel-out",):
                        raise ValueError("Unexpected configured output: " + value)
                    actual = root / relative
                    if not actual.is_file():
                        row["outputs"].setdefault(name, []).append({"path": value, "missing": True})
                        continue
                    with actual.open("rb") as stream:
                        identity = {"sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
                                    "bytes": os.fstat(stream.fileno()).st_size}
                    destination = evidence / (query_phase + "-" + str(index) + "-" + actual.name)
                    if identity["bytes"] > 8 * 1024 * 1024:
                        raise ValueError("Qualification output exceeds 8 MiB")
                    shutil.copyfile(actual, destination)
                    paths[name].append(actual)
                    row["outputs"].setdefault(name, []).append({"path": value, "retained": destination.name, **identity})
            if built["status"] != "PASS":
                failed = True
                continue
            if {p.name for p in paths.get("logs", [])} != {"Configure.log", "build_script.sh", "wrapper_build_script.sh"} or len(paths.get("logs", [])) != 3:
                raise ValueError("Expected the complete foreign_cc scripts and log")
            if len(paths.get("binary", [])) != 1:
                raise ValueError("Expected exactly one configured timeout binary")
            binary = paths["binary"][0]
            events = evidence / ("invocation-" + phase) / "events.jsonl"
            owners = set()
            for line in events.read_text().splitlines():
                identity = json.loads(line).get("id", {}).get("targetCompleted")
                if identity and identity.get("label") == binary_target and not identity.get("aspect"):
                    owners.add((report.Owner.parse(identity), "default"))
            advertised = report.advertised_build_outputs(events, owners)
            if len(advertised) != 1 or advertised[0]["identity"]["label"] != binary_target:
                raise ValueError("Expected one configured binary owner in BEP")
            actual = row["outputs"]["binary"][0]
            if any(actual[key] != advertised[0][key] for key in ("sha256", "bytes")):
                raise ValueError("Configured binary differs from advertised build output")
            row["advertised"] = advertised[0]
            elf = subprocess.run(["readelf", "--file-header", "--program-headers", "--dynamic", str(binary)],
                                 capture_output=True, text=True, timeout=30)
            row["readelf"] = {"native_exit": elf.returncode, "stdout": elf.stdout, "stderr": elf.stderr}
            elf.check_returncode()
            if machine not in elf.stdout or "INTERP" in elf.stdout or "(NEEDED)" in elf.stdout:
                raise ValueError("timeout is not static for its selected architecture")
            row["status"] = "PASS"
        if failed:
            raise RuntimeError("A timeout build or configured output query failed")
        records["status"] = "PASS"
    finally:
        report.write_json(evidence / "coreutils-timeout-build.json", records)
