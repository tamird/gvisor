"""Check clang-tidy against the upstream GNU compilation contexts."""

import json
import os
from pathlib import Path

import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    targets = config["targets"]
    options = ["--compilation_mode=opt", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc",
               "--build_event_json_file_path_conversion=false"]
    records = {"source": config["source"], "status": "INCOMPLETE", "architectures": [],
               "scope": "Positive clang-tidy fixture with both upstream GNU target toolchains"}
    try:
        result = invoke("execution-root", ["info", *options, "execution_root"], "command", targets=[])
        if result["status"] != "PASS":
            raise RuntimeError("Cannot identify canonical execution root")
        root = Path((evidence / "invocation-execution-root/stdout.log").read_text().strip())
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("Invalid canonical execution root")
        for architecture in ("x86_64", "aarch64"):
            selected = [*options, "--config=" + architecture,
                        "--platforms=" + config["image_platforms"][architecture],
                        "--aspects=//tools/clang_tidy:clang_tidy.bzl%clang_tidy",
                        "--output_groups=clang_tidy"]
            phase = "clang-tidy-" + architecture
            row = {"architecture": architecture, "status": "INCOMPLETE"}
            records["architectures"].append(row)
            execution = evidence / (phase + "-execution.json")
            built = invoke(phase, ["build", *selected, "--execution_log_json_file=" + str(execution),
                           "--noexecution_log_sort", *targets], "build", targets=targets)
            row["build_status"] = built["status"]
            if execution.is_file():
                record_execution(execution, architecture, "clang-tidy")
            if built["status"] != "PASS":
                raise RuntimeError("Clang-tidy failed with the upstream GNU compilation context")
            actions = json.loads((evidence / (architecture + "-clang-tidy-image-execution.json")).read_text())
            matching = [action for action in actions["image_actions"] if action["mnemonic"] == "ClangTidy"]
            match matching:
                case [action] if action["targetLabel"] == targets[0]:
                    pass
                case _:
                    raise ValueError(f"Expected one ClangTidy action for {targets}, got {matching}")
            if action["exitCode"] != 0 or action["status"] or not action["digest"]["hash"]:
                raise ValueError("Clang-tidy action failed or omitted its digest")
            match action["actualOutputs"]:
                case [{"path": str(path), "digest": {"hash": str(digest), **details}}]:
                    expected = {"sha256": digest, "bytes": int(details.get("sizeBytes", 0))}
                case _:
                    raise ValueError("Expected exactly one declared clang-tidy report")
            relative = Path(path)
            if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("bazel-out",):
                raise ValueError("Unexpected configured clang-tidy report: " + path)
            actual = report.file_record(root / relative)
            if actual != expected:
                raise ValueError(f"Clang-tidy report differs from native action identity: {actual} != {expected}")
            row["action"] = action
            row["report"] = {"path": path, **actual}
            row["status"] = "PASS"
        records["status"] = "PASS"
    finally:
        report.write_json(evidence / "clang-tidy-gnu-compatibility.json", records)
