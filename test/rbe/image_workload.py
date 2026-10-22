"""Compare configured image archives and contexts with qualified output bytes."""
import hashlib
import json
import os
from pathlib import Path

import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    targets = config["targets"]
    expected = config["expected_outputs"]
    record = {"status": "INCOMPLETE", "source": config["source"],
              "scope": "Exact archive/context equality; no runtime replay or fresh-execution assumption",
              "query_phases": [], "architectures": []}
    options = ["--compilation_mode=opt", "--lockfile_mode=error", "--noannounce_rc",
               "--downloader_config=" + os.environ["APPLICABILITY_DOWNLOADER_CONFIG"]]

    def query(name: str, arguments: list[str]) -> str:
        result = invoke(name, arguments, "command", targets=[])
        record["query_phases"].append(name)
        if result["status"] != "PASS":
            raise RuntimeError("Configured output query failed: " + name)
        return (evidence / ("invocation-" + name) / "stdout.log").read_text()

    try:
        execution_root = Path(query("output-execution-root", ["info", *options, "execution_root"]).strip())
        if not execution_root.is_absolute() or not execution_root.is_dir():
            raise ValueError("Invalid canonical execution root")
        for architecture in ("x86_64", "aarch64"):
            row = {"architecture": architecture, "status": "INCOMPLETE", "outputs": []}
            architecture_options = config["architecture_options"][architecture]
            record["architectures"].append(row)
            execution = private / (architecture + "-execution.json")
            phase = architecture + "-images"
            built = invoke(phase, ["build", "--config=" + architecture, *options, *architecture_options,
                           "--remote_download_outputs=all",
                           "--build_event_json_file_path_conversion=false",
                           "--execution_log_json_file=" + str(execution),
                           "--noexecution_log_sort", *targets], "build")
            if execution.is_file():
                record_execution(execution, architecture)
            if built["status"] != "PASS":
                raise RuntimeError("Image build did not pass: " + architecture)
            events = evidence / ("invocation-" + phase) / "events.jsonl"
            groups = set()
            for line in events.read_text().splitlines():
                event = json.loads(line)
                identity = event.get("id", {}).get("targetCompleted")
                if identity and identity.get("label") in targets and not identity.get("aspect"):
                    groups.add((report.Owner.parse(identity), "default"))
            advertised = report.advertised_build_outputs(events, groups)
            by_label = {item["identity"]["label"]: item for item in advertised}
            if len(advertised) != len(targets) or set(by_label) != set(targets):
                raise ValueError("Expected exactly one advertised output for each configured owner")
            query_text = query(architecture + "-output-paths", ["cquery", *options, "--config=" + architecture, *architecture_options,
                                "--output=starlark", "--starlark:expr=" +
                                "json.encode([target.label.name, [f.path for f in target.files.to_list()]])",
                                "set(" + " ".join(targets) + ")"])
            paths = {}
            for line in query_text.splitlines():
                match json.loads(line):
                    case [str(name), [str(value)]]:
                        path = Path(value)
                        if name in paths or path.is_absolute() or ".." in path.parts or path.parts[:1] != ("bazel-out",):
                            raise ValueError("Unexpected configured output: " + line)
                        paths[name] = execution_root / path
                    case _:
                        raise ValueError("Expected a named single output: " + line)
            if set(paths) != {label.split(":")[1] for label in targets}:
                raise ValueError("Configured output set differs")
            for label in targets:
                path = paths[label.split(":")[1]]
                with path.open("rb") as stream:
                    actual = {"sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
                              "bytes": os.fstat(stream.fileno()).st_size}
                item = by_label[label]
                wanted = expected[architecture][label]
                row["outputs"].append({"owner": label, "path": str(path),
                                       "actual": actual, "advertised": item, "expected": wanted})
                if actual != wanted or actual != {key: item[key] for key in ("sha256", "bytes")}:
                    raise ValueError("Qualified output identity differs: " + architecture + " " + label)
            row["status"] = "PASS"
        record["status"] = "PASS"
    finally:
        report.write_json(evidence / "image-output-equivalence.json", record)
