"""Check the compiler migration through its existing build and test owners."""

import json
import os
from pathlib import Path
import re

import qualification_report as report


def validate_graph(invoke, evidence: Path, options: list[str], groups: list[dict]) -> dict:
    """Compare the transitioned owners with the existing four direct queries."""
    formatter = str(Path.cwd() / "test/rbe/compiler_matrix/options.cquery")

    def query(name: str, expression: str, extra: list[str]) -> list[dict]:
        result = invoke(name, ["cquery", *options, *extra, "--output=starlark",
                              "--starlark:file=" + formatter, expression], "command", targets=[])
        if result["status"] != "PASS":
            raise RuntimeError("Configured compiler graph query failed: " + name)
        return [json.loads(line) for line in
                (evidence / ("invocation-" + name) / "stdout.log").read_text().splitlines()]

    def settings(row: dict) -> dict:
        selected = {}
        for name in ("cpu", "platforms", "race", "pure"):
            suffix = ("//command_line_option:" if name in ("cpu", "platforms") else "//go/config:") + name
            matches = [value for key, value in row["options"].items() if key.endswith(suffix)]
            match matches:
                case [value]:
                    selected[name] = value
                case _:
                    raise ValueError(f"Expected one {name} setting for {row['label']}: {matches}")
        return selected

    roots = {root: target for group in groups for root, target in group["roots"].items()}
    labels = sorted(set(roots) | set(roots.values()))
    expression = "filter(" + json.dumps("^(" + "|".join(re.escape(label) for label in labels) + ")$")
    expression += ", deps(set(" + " ".join(roots) + ")))"
    combined = query("compiler-matrix-graph", expression, [])
    joins = []
    for group in groups:
        direct = query("compiler-reference-" + group["name"],
                       "set(" + " ".join(group["targets"]) + ")", group["options"])
        for root, target in group["roots"].items():
            matches = [row for row in combined if row["label"] == root]
            match matches:
                case [wrapper] if len(wrapper["files"]) == 1:
                    pass
                case _:
                    raise ValueError(f"Expected one single-output matrix root {root}: {matches}")
            reference = [row for row in direct if row["label"] == target and row["files"] == wrapper["files"]]
            match reference:
                case [original]:
                    pass
                case _:
                    raise ValueError(f"Matrix output differs from direct owner {target}: {wrapper}, {reference}")
            origin = [row for row in combined if row["label"] == target and
                      row["files"] == original["files"] and settings(row) == settings(original)]
            if not origin:
                raise ValueError(f"Matrix has no matching configured original owner: {original}")
            joins.append({"group": group["name"], "root": root, "original_target": target,
                          "files": original["files"], "settings": settings(original)})
    if len(joins) != len(roots) or len(roots) != 40:
        raise ValueError(f"Expected all 40 configured compiler roots, got {len(joins)}")
    return {"status": "PASS", "configured_outputs": joins,
            "scope": "Configured graph and output-path equality only; no compiler output was built or inspected"}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    options = ["--compilation_mode=opt", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc",
               "--build_event_json_file_path_conversion=false"]
    records = {"source": config["source"], "status": "INCOMPLETE", "builds": [], "tests": []}
    failed = False
    try:
        if config.get("format_only", False):
            records["status"] = "PASS"
            return
        if config.get("graph_only", False):
            records["graph_validation"] = validate_graph(invoke, evidence, options, config["build_groups"])
            records["status"] = "PASS"
            return
        result = invoke("execution-root", ["info", *options, "execution_root"], "command", targets=[])
        if result["status"] != "PASS":
            raise RuntimeError("Cannot identify canonical execution root")
        root = Path((evidence / "invocation-execution-root/stdout.log").read_text().strip())
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("Invalid canonical execution root")
        groups = {group["name"]: group for group in config["build_groups"]}
        for batch in config["build_batches"]:
            selected = [groups[name] for name in batch["groups"]]
            rows = [{**group, "status": "INCOMPLETE", "phase": batch["name"]} for group in selected]
            records["builds"].extend(rows)
            roots = [root for group in selected for root in group["roots"]]
            phase = batch["name"]
            # Unprocessed or incomplete raw logs must not enter the compact
            # evidence scan or bundle if the recorder fails.
            execution = private / (phase + "-execution.json")
            built = invoke(phase, ["build", *options, *batch["options"], "--keep_going",
                           "--execution_log_json_file=" + str(execution), "--noexecution_log_sort",
                           *roots], "build", targets=roots)
            for row in rows:
                row["build_status"] = built["status"]
            if execution.is_file():
                try:
                    record_execution(execution, batch["architecture"], phase)
                except (OSError, ValueError, RuntimeError) as error:
                    # The shared recorder retains runner/capture FAIL. Continue
                    # unrelated groups without granting execution proof here.
                    for row in rows:
                        row["execution_error"] = str(error)
                    failed = True
            else:
                for row in rows:
                    row["execution_error"] = "Native execution log missing"
                failed = True
            if built["status"] != "PASS":
                failed = True
                continue
            try:
                owners = {(report.Owner(owner["label"], owner["configuration"]), "default")
                          for owner in built["expected"]}
                outputs = report.advertised_build_outputs(evidence / ("invocation-" + phase) / "events.jsonl", owners)
                if len(outputs) != len(roots) or {out["identity"]["label"] for out in outputs} != set(roots):
                    raise ValueError("Expected one default executable or library per configured owner")
                for output in outputs:
                    relative = Path(output["name"])
                    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("bazel-out",):
                        raise ValueError("Unexpected configured output: " + output["name"])
                    actual = report.file_record(root / relative)
                    if actual != {key: output[key] for key in ("sha256", "bytes")}:
                        raise ValueError("Materialized output differs from default BEP identity")
                    output["materialized_path"] = str(root / relative)
                for row in rows:
                    row["outputs"] = [{**output, "original_target": row["roots"][output["identity"]["label"]]}
                                      for output in outputs if output["identity"]["label"] in row["roots"]]
                    if {out["original_target"] for out in row["outputs"]} != set(row["targets"]):
                        raise ValueError("Configured roots do not cover the original group: " + row["name"])
                    row["status"] = "PASS"
            except (OSError, ValueError) as error:
                for row in rows:
                    row["output_error"] = str(error)
                failed = True
        # Every successful build batch retains its outputs even if another batch
        # failed. The inspector consumes only complete owner sets.
        manifest = evidence / "compiler-builds.json"
        report.write_json(manifest, records)
        inspected = invoke("compiler-output-inspection",
                           ["run", *options, "@rules_python//python/bin:python", "--",
                            str(Path.cwd() / "test/rbe/compiler_inspect.py"), str(manifest),
                            str(evidence / "compiler-outputs.json")], "command", targets=[])
        failed |= inspected["status"] != "PASS"
        for group in config["test_groups"]:
            result = invoke(group["name"], ["test", *options, *group["options"], "--keep_going",
                            "--nocache_test_results", "--test_output=errors", *group["targets"]],
                            "test", targets=group["targets"])
            records["tests"].append({**group, "status": result["status"]})
            failed |= result["status"] != "PASS"
        if failed:
            raise RuntimeError("One or more compiler build, output, or owning test gates failed")
        records["status"] = "PASS"
    finally:
        report.write_json(evidence / "compiler-current-build.json", records)
