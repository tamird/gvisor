"""Check the compiler migration through its existing build and test owners."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request

import qualification_report as report


def prepare_graph_recovery(config: dict, evidence: Path, private: Path) -> dict:
    """Reuse exact prior native queries and lock; keep the prior overall failure."""
    supplier = config["retained_graph"]
    changed = subprocess.check_output(
        ["git", "diff", "--name-only", supplier["source"], config["source"]],
        text=True, timeout=30).splitlines()
    if set(changed) != {"test/rbe/image_workload.py", "test/rbe/image_qualification.py"}:
        raise ValueError(f"Graph recovery changed compiled or formatter inputs: {changed}")
    expected = supplier["artifact"]
    request = urllib.request.Request(
        "https://app.buildbuddy.io/api/v1/GetFile",
        data=json.dumps({"uri": expected["uri"]}).encode(),
        headers={"Content-Type": "application/json",
                 "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
    deadline = float(os.environ["APPLICABILITY_WORK_DEADLINE"])
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Retained graph retrieval deadline exhausted")
    artifact = private / "retained-compiler-graph.tar.gz"
    size = 0
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=min(60, remaining)) as response, artifact.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > expected["bytes"] or time.monotonic() >= deadline:
                raise ValueError("Retained graph exceeded its size or time bound")
            digest.update(chunk)
            output.write(chunk)
    if (size, digest.hexdigest()) != (expected["bytes"], expected["sha256"]):
        raise ValueError("Retained graph archive identity differs")
    shutil.copyfile(artifact, evidence / artifact.name)
    with tarfile.open(artifact) as archive:
        def member(name: str) -> bytes:
            entries = [entry for entry in archive.getmembers() if entry.name == name]
            match entries:
                case [entry] if entry.isfile():
                    return archive.extractfile(entry).read()
                case _:
                    raise ValueError("Expected one regular retained graph member: " + name)

        def command(name: str) -> bytes:
            directory = "invocation-" + name + "/"
            native = json.loads(member(directory + "verdict.json"))
            if native["source"] != supplier["source"] or native["status"] != "PASS" or native["native_exit"] != 0:
                raise ValueError("Retained native query/formatter failed: " + name)
            for stream in native["command_outputs"].values():
                raw = member(directory + stream["path"])
                if (len(raw), hashlib.sha256(raw).hexdigest()) != (stream["bytes"], stream["sha256"]):
                    raise ValueError("Retained command stream identity differs: " + name)
            return member(directory + "stdout.log")

        if command("00-buildifier"):
            raise ValueError("Retained formatter proposed source changes")
        combined = [json.loads(line) for line in command("compiler-matrix-graph").splitlines()]
        direct = [json.loads(line) for line in command("compiler-reference-x86_64-normal").splitlines()]
        lock = member(supplier["lock"]["member"])
        if (len(lock), hashlib.sha256(lock).hexdigest()) != (supplier["lock"]["bytes"], supplier["lock"]["sha256"]):
            raise ValueError("Retained graph lock identity differs")
        for name in ("00-buildifier", "compiler-matrix-graph", "compiler-reference-x86_64-normal"):
            if any(member("invocation-" + name + "/lock-" + suffix) != lock for suffix in ("before", "after")):
                raise ValueError("Retained graph consumer changed the lock: " + name)
        prior = json.loads(member("compiler-current-build.json"))
        if prior != {"source": supplier["source"], "status": "INCOMPLETE", "builds": [], "tests": []}:
            raise ValueError("Unexpected retained producer record")
    Path("MODULE.bazel.lock").write_bytes(lock)
    report.write_json(evidence / "retained-graph-provenance.json", {
        "supplier": supplier, "prior_producer": prior, "changed_paths": changed,
        "reused_native_phases": ["00-buildifier", "compiler-matrix-graph", "compiler-reference-x86_64-normal"],
        "scope": "Reuse successful individual phases; original overall FAIL remains unchanged."})
    return {"combined": combined, "references": {"x86_64-normal": direct}}


def source_label(label: str) -> str:
    # This is only canonical spelling for the main repository, not alias
    # resolution. Never erase an external repository or a source-owned suffix.
    if label.startswith("@@//"):
        return label[2:]
    if label.startswith("//"):
        return label
    raise ValueError(f"Unexpected source repository: {label}")


def graph_settings(row: dict) -> dict:
    native = {"cpu": "//command_line_option:cpu", "platforms": "//command_line_option:platforms"}
    booleans = {name: "@@rules_go+//go/config:" + name for name in ("race", "pure")}
    options = row["options"]
    if set(options) - (set(native.values()) | set(booleans.values())):
        raise ValueError(f"Unknown graph option owner or setting: {options}")
    result = {}
    for name, key in native.items():
        value = options.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Missing required native setting {name}: {value!r}")
        result[name] = value
    for name, key in booleans.items():
        # Bazel 8.8.1 omits default Starlark values. These exact rules_go 0.63
        # flags default to False; a missing native option remains an error.
        value = options.get(key, "False")
        if value not in ("True", "False"):
            raise ValueError(f"Invalid {name} setting: {value!r}")
        result[name] = value == "True"
    return result


def graph_output(row: dict) -> tuple[str, str]:
    match row["files"]:
        case [path] if isinstance(path, str):
            parts = PurePosixPath(path).parts
        case other:
            raise ValueError(f"Expected one configured output: {other!r}")
    if (".." in parts or len(parts) < 4 or parts[0] != "bazel-out" or parts[2] != "bin" or
            not re.fullmatch(r"(k8|aarch64)-opt(?:-ST-[0-9a-f]{12})?", parts[1])):
        raise ValueError(f"Unexpected configured output path: {path}")
    return parts[1], "/".join(parts[3:])


def validate_graph(invoke, evidence: Path, options: list[str], groups: list[dict], retained: dict) -> dict:
    """Join every wrapper to its source owner and direct-reference settings."""
    formatter = str(Path.cwd() / "test/rbe/compiler_matrix/options.cquery")

    def query(name: str, expression: str, extra: list[str]) -> list[dict]:
        result = invoke(name, ["cquery", *options, *extra, "--output=starlark",
                              "--starlark:file=" + formatter, expression], "command", targets=[])
        if result["status"] != "PASS":
            raise RuntimeError("Configured compiler graph query failed: " + name)
        return [json.loads(line) for line in
                (evidence / ("invocation-" + name) / "stdout.log").read_text().splitlines()]

    roots = {root: target for group in groups for root, target in group["roots"].items()}
    root_set = "set(" + " ".join(roots) + ")"
    # Query strings do not process JSON escapes. Only shim was missing from the
    # old original-owner rows; do not repeat that full dependency query.
    expression = root_set + ' union filter("^//shim:containerd-shim-runsc-v1$", deps(' + root_set + "))"
    recovered = query("compiler-matrix-missing", expression, [])
    combined = retained["combined"] + recovered
    joins = []
    for group in groups:
        direct = retained["references"].get(group["name"])
        if direct is None:
            direct = query("compiler-reference-" + group["name"],
                           "set(" + " ".join(group["targets"]) + ")", group["options"])
        cpu = "k8" if group["architecture"] == "x86_64" else "aarch64"
        race = group["name"].endswith("-race")
        for root, target in group["roots"].items():
            # Pinned with_cfg 1.0 uses a native.alias frontend for filegroups.
            # Bind those exact source-owned names; do not strip arbitrary suffixes.
            wrappers = [row for row in recovered if source_label(row["label"]) == root + "_with_cfg"]
            match wrappers:
                case [wrapper]:
                    _, relative = graph_output(wrapper)
                case _:
                    raise ValueError(f"Expected one explicit matrix wrapper {root}: {wrappers}")
            actual = target + "_with_cfg" if target == "//tools/ioctl_sniffer:ioctl_hook_gnu" else target
            references = [row for row in direct if source_label(row["label"]) == actual and
                          graph_settings(row)["cpu"] == cpu and graph_settings(row)["race"] == race and
                          graph_settings(row)["pure"] != race]
            names = {graph_output(row)[1] for row in references}
            if names != {relative}:
                raise ValueError(f"Missing or ambiguous direct output for {target}: {references}")
            # cquery returns aliases and actual configured targets. Preserve all
            # distinct effective-setting variants, and require their full set to
            # agree; do not choose a convenient duplicate or ignore an extra one.
            candidates = [row for row in combined if source_label(row["label"]) == actual and
                          row["files"] == wrapper["files"]]
            variants = lambda rows: {json.dumps(graph_settings(row), sort_keys=True) for row in rows}
            if not candidates or variants(candidates) != variants(references):
                raise ValueError(f"Configured owner/settings differ for {target}: {candidates}, {references}")
            joins.append({"group": group["name"], "root": root, "original_target": target,
                          "actual_label": actual, "wrapper": wrapper, "combined_owners": candidates,
                          "direct_owners": references, "relative_output": relative})
    if len(joins) != len(roots) or len(roots) != 40:
        raise ValueError(f"Expected all 40 configured compiler roots, got {len(joins)}")
    return {"status": "PASS", "configured_outputs": joins,
            "scope": "Source-owner, effective transition-setting and relative-output equality. Configuration directories retained; no binary/action/cache equivalence or build credit."}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path, retained_graph: dict | None = None) -> None:
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
            if retained_graph is None:
                raise ValueError("Graph recovery requires the authenticated prior evidence")
            records["graph_validation"] = validate_graph(invoke, evidence, options, config["build_groups"], retained_graph)
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
