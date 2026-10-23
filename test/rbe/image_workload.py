"""Qualify the LLVM source supplier through the NVIDIA parser's owners."""

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import zipfile

import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    options = ["--compilation_mode=opt", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc",
               "--build_event_json_file_path_conversion=false"]
    records = {"source": config["source"], "product": config["product"],
               "status": "INCOMPLETE", "builds": [], "tests": []}
    failed = False
    resource_root = private / "clang-resources"
    resource_headers = {}
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
            shutil.copyfile(output_root / relative / source, evidence / name)
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
                    if output["identity"]["label"] == config["header_archive_target"]:
                        with zipfile.ZipFile(root / relative) as archive:
                            for member in archive.infolist():
                                member_path = Path(member.filename)
                                if member_path.is_absolute() or ".." in member_path.parts or member_path.parts[:1] != ("include",) and member.filename != "CLANG-LICENSE.TXT":
                                    raise ValueError("Unexpected builtin header archive member")
                                if not member.is_dir():
                                    data = archive.read(member)
                                    resource_headers[member.filename] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                            if not {"include/stdint.h", "include/stddef.h", "CLANG-LICENSE.TXT"} <= resource_headers.keys():
                                raise ValueError("Required builtin headers missing from archive")
                            if resource_headers["CLANG-LICENSE.TXT"]["sha256"] != "ebcd9bbf783a73d05c53ba4d586b8d5813dcdf3bbec50265860ccc885e606f47":
                                raise ValueError("Clang resource license differs from source archive")
                            archive.extractall(resource_root)
                        output["format"] = "zip"
                        records["header_archive"] = {**output, "members": resource_headers}
                    else:
                        with (root / relative).open("rb") as binary:
                            header = binary.read(20)
                        if header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != group["elf_machine"]:
                            raise ValueError("Configured binary ELF architecture differs")
                        output["elf_machine"] = group["elf_machine"]
                row["status"] = "PASS"
            except (OSError, ValueError) as error:
                row["output_error"] = str(error)
                failed = True

        # Exercise the same explicit resource directory and host-header
        # exclusion as Runner. Join every reached header to the declared ZIP.
        source = evidence / "header-probe.cc"
        source.write_text("#include <stdint.h>\n#include <stddef.h>\n"
                          "typedef struct { uint64_t value; size_t size; } HeaderRecord;\n"
                          "const unsigned long HeaderSize = sizeof(HeaderRecord);\n")
        parser_input = evidence / "header-probe-input.json"
        report.write_json(parser_input, {"structs": ["HeaderRecord"], "constants": ["HeaderSize"]})
        result = invoke("parser-header-search", ["run", *options, "--config=x86_64",
                        "//tools/nvidia_driver_differ:driver_ast_parser", "--",
                        "--input", str(parser_input), "--extra-arg=-v", "--extra-arg=-H",
                        "--extra-arg=-resource-dir=" + str(resource_root),
                        "--extra-arg=-nostdlibinc", "--extra-arg=-nostdinc++", str(source)], "command", targets=[])
        records["header_search_status"] = result["status"]
        if result["status"] == "PASS":
            parsed = json.loads((evidence / "invocation-parser-header-search/stdout.log").read_text())
            if parsed["constants"] != {"HeaderSize": 16} or parsed["records"]["HeaderRecord"]["size"] != 16:
                raise ValueError("Parser builtin-header layout differs")
            headers = []
            diagnostics = (evidence / "invocation-parser-header-search/stderr.log").read_text()
            for line in diagnostics.splitlines():
                if match := re.fullmatch(r"\.+ (/.*)", line):
                    header = Path(match[1])
                    name = str(header.relative_to(resource_root))
                    actual = report.file_record(header)
                    if resource_headers.get(name) != actual:
                        raise ValueError("Reached header is not the declared archive member: " + name)
                    headers.append({"path": str(header), "member": name, **actual})
            if not {"stdint.h", "stddef.h"} <= {Path(header["path"]).name for header in headers}:
                raise ValueError("Actual stdint.h/stddef.h header provenance missing")
            records["runtime_headers"] = headers
        else:
            failed = True

        # Exercise run_differ's detached parser and its embedded source headers
        # through a real supported NVIDIA version.
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
