"""Qualify the public libbpf headers through their existing BPF consumers."""
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
            execution = evidence / (phase + "-execution.json")
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
            row["status"] = "PASS"
        records["status"] = "PASS"
    finally:
        report.write_json(evidence / "bpf-headers-build.json", records)
