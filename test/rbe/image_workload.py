"""Qualify the LLVM source supplier through the NVIDIA parser's owners."""

import ast
import json
import os
from pathlib import Path
import re
import shutil

import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    options = ["--compilation_mode=opt", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc",
               "--build_event_json_file_path_conversion=false"]
    records = {"source": config["source"], "product": config["product"],
               "status": "INCOMPLETE", "builds": [], "tests": []}
    failed = False
    try:
        result = invoke("execution-root", ["info", *options, "execution_root"], "command", targets=[])
        if result["status"] != "PASS":
            raise RuntimeError("Cannot identify canonical execution root")
        root = Path((evidence / "invocation-execution-root/stdout.log").read_text().strip())
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("Invalid canonical execution root")

        result = invoke("llvm-source-root", ["cquery", *options, "--config=x86_64",
                        "@llvm-project//llvm:Support", "--output=starlark",
                        "--starlark:expr=target.label.workspace_root"], "command", targets=[])
        if result["status"] != "PASS":
            raise RuntimeError("Cannot identify configured LLVM source repository")
        paths = (evidence / "invocation-llvm-source-root/stdout.log").read_text().splitlines()
        match paths:
            case [path]:
                relative = Path(path)
            case _:
                raise ValueError("Expected exactly one configured LLVM source repository")
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("external",):
            raise ValueError("Unexpected configured LLVM source repository")
        for name, source in (("llvm-targets.bzl", "llvm/targets.bzl"),
                             ("llvm-clang.BUILD.bazel", "clang/BUILD.bazel")):
            shutil.copyfile(root / relative / source, evidence / name)
        targets = ast.literal_eval((evidence / "llvm-targets.bzl").read_text().split("=", 1)[1].strip())
        if targets != config["llvm_targets"]:
            raise ValueError(f"Configured LLVM targets differ: {targets!r}")
        records["llvm_source_repository"] = {"path": path, "targets": targets}

        for group in config["build_groups"]:
            phase = group["name"]
            row = {**group, "status": "INCOMPLETE"}
            records["builds"].append(row)
            execution = private / (phase + "-execution.json")
            built = invoke(phase, ["build", *options, *group["options"], "--keep_going",
                           "--execution_log_json_file=" + str(execution), "--noexecution_log_sort",
                           *group["targets"]], "build", targets=group["targets"])
            row["build_status"] = built["status"]
            if execution.is_file():
                try:
                    record_execution(execution, group["architecture"], phase)
                except (OSError, ValueError, RuntimeError) as error:
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
                    raise ValueError("Expected one default binary per configured owner")
                row["outputs"] = outputs
                for output in outputs:
                    relative = Path(output["name"])
                    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("bazel-out",):
                        raise ValueError("Unexpected configured output: " + output["name"])
                    actual = report.file_record(root / relative)
                    if actual != {key: output[key] for key in ("sha256", "bytes")}:
                        raise ValueError("Materialized output differs from default BEP identity")
                    with (root / relative).open("rb") as binary:
                        header = binary.read(20)
                    if header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != group["elf_machine"]:
                        raise ValueError("Configured binary ELF architecture differs")
                    output["elf_machine"] = group["elf_machine"]
                row["status"] = "PASS"
            except (OSError, ValueError) as error:
                row["output_error"] = str(error)
                failed = True

        # Exercise the default header lookup used by ParseDriver. Verbose
        # diagnostics retain its actual resource and system include paths;
        # success alone is not a claim that runtime headers are hermetic.
        source = evidence / "header-probe.cc"
        source.write_text("#include <stdint.h>\n#include <stddef.h>\n"
                          "struct HeaderRecord { uint64_t value; size_t size; };\n"
                          "const unsigned long HeaderSize = sizeof(HeaderRecord);\n")
        parser_input = evidence / "header-probe-input.json"
        report.write_json(parser_input, {"structs": ["HeaderRecord"], "constants": ["HeaderSize"]})
        result = invoke("parser-header-search", ["run", *options, "--config=x86_64",
                        "//tools/nvidia_driver_differ:driver_ast_parser", "--",
                        "--input", str(parser_input), "--extra-arg=-v", "--extra-arg=-H", str(source)], "command", targets=[])
        records["header_search_status"] = result["status"]
        if result["status"] == "PASS":
            parsed = json.loads((evidence / "invocation-parser-header-search/stdout.log").read_text())
            if parsed["constants"] != {"HeaderSize": 16} or parsed["records"]["HeaderRecord"]["size"] != 16:
                raise ValueError("Default parser header layout differs")
            headers = []
            diagnostics = (evidence / "invocation-parser-header-search/stderr.log").read_text()
            for line in diagnostics.splitlines():
                if match := re.fullmatch(r"\.+ (/.*)", line):
                    header = Path(match[1])
                    headers.append({"path": str(header), **report.file_record(header)})
            if not {"stdint.h", "stddef.h"} <= {Path(header["path"]).name for header in headers}:
                raise ValueError("Actual stdint.h/stddef.h header provenance missing")
            records["runtime_headers"] = headers
        else:
            failed = True

        # run_differ extracts only the embedded parser bytes into a temporary
        # path. Exercise that real path as well as the Bazel data dependency.
        result = invoke("embedded-parser-driver", ["run", *options, "--config=x86_64",
                        "//tools/nvidia_driver_differ:run_differ", "--",
                        "-base=" + config["nvidia_driver_version"],
                        "-next=" + config["nvidia_driver_version"]], "command", targets=[])
        records["embedded_driver_status"] = result["status"]
        failed |= result["status"] != "PASS"

        for group in config["test_groups"]:
            result = invoke(group["name"], ["test", *options, *group["options"], "--keep_going",
                            "--nocache_test_results", "--test_output=all", *group["targets"]],
                            "test", targets=group["targets"])
            records["tests"].append({**group, "status": result["status"], "case_counts": result["case_counts"]})
            failed |= result["status"] != "PASS" or result["case_counts"] != group["expected_cases"]
        if failed:
            raise RuntimeError("One or more LLVM source or NVIDIA parser gates failed")
        records["status"] = "PASS"
    finally:
        report.write_json(evidence / "compiler-llvm-source.json", records)
