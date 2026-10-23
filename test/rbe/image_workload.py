"""Qualify the configured Linux UAPI selection through existing BPF consumers."""
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
               "scope": "Configured BPF closures and outputs on both targets; no runtime claim"}
    try:
        registry = config["libbpf_registry"]
        lock = json.loads(Path("MODULE.bazel.lock").read_text())
        if lock["registryFileHashes"].get(registry["source_url"]) != registry["source_sha256"]:
            raise ValueError("Resolved libbpf registry source differs from reviewed archive metadata")
        repository = output_root / "external/libbpf+"
        materialized = {name: report.file_record(repository / name) for name in registry["materialized"]}
        if materialized != registry["materialized"]:
            raise ValueError("Materialized libbpf MODULE or BUILD differs from the reviewed registry revision")
        records["libbpf_registry"] = {**registry, "materialized": materialized,
                                     "lock": report.file_record(Path("MODULE.bazel.lock"))}
        header_config = config["kernel_headers"]
        index = output_root / "external/llvm+/kernel/extension/kernel_headers_index.json"
        if report.file_record(index) != header_config["index"]:
            raise ValueError("Kernel header index differs from the pinned supplier")
        entries = json.loads(index.read_text())[header_config["version"]]
        if {arch: entries[arch] for arch in ("x86", "arm64")} != header_config["archives"]:
            raise ValueError("Kernel header archive metadata differs from the pinned selection")
        records["kernel_headers"] = header_config
        result = invoke("execution-root", ["info", *options, "execution_root"], "command", targets=[])
        if result["status"] != "PASS":
            raise RuntimeError("Cannot identify canonical execution root")
        root = Path((evidence / "invocation-execution-root/stdout.log").read_text().strip())
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("Invalid canonical execution root")
        for architecture in ("x86_64", "aarch64"):
            selected = [*options, "--config=" + architecture,
                        "--platforms=" + config["image_platforms"][architecture]]
            phase = "bpf-" + architecture
            row = {"architecture": architecture, "status": "INCOMPLETE"}
            records["architectures"].append(row)
            queried = invoke(phase + "-closure", ["cquery", *selected, "--output=label",
                             "deps(set(" + " ".join(targets) + "))"], "command", targets=[])
            if queried["status"] != "PASS":
                raise RuntimeError("Configured BPF closure query failed")
            lines = (evidence / ("invocation-" + phase + "-closure") / "stdout.log").read_text().splitlines()
            labels = {line.split()[0].removeprefix("@@//").removeprefix("@//") for line in lines if line.strip()}
            labels = {"//" + label if not label.startswith(("@", "//")) else label for label in labels}
            missing = set(targets) - labels
            forbidden = {label for label in labels if label.startswith(("@elfutils//", "@@elfutils+//"))
                         or label.endswith("//:libbpf_impl")}
            headers = {label for label in labels if label.endswith("//:bpf_headers")}
            row["closure"] = {"missing": sorted(missing), "forbidden": sorted(forbidden),
                              "header_targets": sorted(headers), "configured_labels": len(labels)}
            if missing or forbidden or len(headers) != 1:
                raise ValueError(f"Incorrect BPF dependency graph: {row['closure']}")
            kernel_arch = "x86" if architecture == "x86_64" else "arm64"
            repository = "@@llvm++kernel_headers+linux_kernel_headers_" + kernel_arch + "." + header_config["version"]
            selected_repositories = {label.split("//", 1)[0] for label in labels
                                     if "+linux_kernel_headers_" in label}
            row["kernel_header_repositories"] = sorted(selected_repositories)
            if selected_repositories != {repository}:
                raise ValueError("BPF graph did not select the expected architecture and Linux headers")
            execution = private / (phase + "-execution.json")
            built = invoke(phase, ["build", *selected, "--execution_log_json_file=" + str(execution),
                           "--noexecution_log_sort", *targets], "build", targets=targets)
            row["build_status"] = built["status"]
            if execution.is_file():
                record_execution(execution, architecture, "bpf")
            if built["status"] != "PASS":
                raise RuntimeError("Configured BPF build failed")
            owners = {(report.Owner(owner["label"], owner["configuration"]), "default")
                      for owner in built["expected"]}
            outputs = report.advertised_build_outputs(evidence / ("invocation-" + phase) / "events.jsonl", owners)
            if len(outputs) != len(targets) or {output["identity"]["label"] for output in outputs} != set(targets):
                raise ValueError("Expected one default BPF output per configured owner")
            row["outputs"] = outputs
            for output in outputs:
                relative = Path(output["name"])
                if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("bazel-out",):
                    raise ValueError("Unexpected configured BPF output: " + output["name"])
                actual = report.file_record(root / relative)
                if actual != {key: output[key] for key in ("sha256", "bytes")}:
                    raise ValueError("Materialized BPF output differs from default BEP identity")
            proof = json.loads((evidence / (architecture + "-bpf-image-execution.json")).read_text())
            actions = [action for action in proof["image_actions"] if action["mnemonic"] == "BPFCompile"]
            if proof["status"] != "PASS" or len(actions) != len(targets) or {action["targetLabel"] for action in actions} != set(targets):
                raise ValueError("Missing or ambiguous configured BPF compile action")
            for action in actions:
                arguments = action["commandArgs"]
                include = "external/" + repository.removeprefix("@@") + "/include"
                pairs = list(zip(arguments, arguments[1:]))
                if ("-isystem", include) not in pairs:
                    raise ValueError("BPF compile command omitted the selected kernel headers")
                output = next(output for output in outputs if output["identity"]["label"] == action["targetLabel"])
                matches = [value for value in action["actualOutputs"] if value["path"] == output["name"]]
                if len(matches) != 1 or matches[0]["digest"]["hash"] != output["sha256"] or int(matches[0]["digest"]["sizeBytes"]) != output["bytes"]:
                    raise ValueError("BPF action output differs from the configured BEP output")
            row["native_header_actions"] = len(actions)
            row["status"] = "PASS"
        records["status"] = "PASS"
    finally:
        report.write_json(evidence / "bpf-headers-build.json", records)
