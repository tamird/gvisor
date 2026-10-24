"""Prepare the exact two-platform Nginx fixture graph before runtime work."""
import json
import os
from pathlib import Path


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    if not config.get("graph_only"):
        raise ValueError("This prepared source is restricted to graph qualification")
    options = ["--compilation_mode=opt", "--config=x86_64", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc"]
    scripts = Path(__file__).parent
    result = invoke("canonical-execution-root", ["info", *options, "execution_root"], "command", targets=[])
    if result["status"] != "PASS":
        raise RuntimeError("Cannot identify the canonical execution root")
    execution_root = Path((evidence / "invocation-canonical-execution-root/stdout.log").read_text().strip())
    if not execution_root.is_absolute() or not execution_root.is_dir():
        raise ValueError("Invalid canonical execution root")
    targets = config["targets"]
    query = 'filter("^(@@)?//images/benchmarks:", deps(set(' + " ".join(targets) + ')))'
    queried = invoke("mixed-image-owners", ["cquery", *options, "--output=starlark",
                     "--starlark:file=" + str(scripts / "image_query.bzl"), query], "command", targets=[])
    if queried["status"] != "PASS":
        raise RuntimeError("Nginx fixture owner query failed")
    rows = [json.loads(line) for line in (evidence / "invocation-mixed-image-owners/stdout.log").read_text().splitlines()]
    graph = {"source": config["source"], "execution_root": str(execution_root), "output_root": str(output_root), "rows": rows}
    (evidence / "image-configured-inputs.json").write_text(json.dumps(graph, indent=2) + "\n")
    labels = {"//images/benchmarks:nginx_" + kind for kind in ("archive", "context", "index")}
    labels |= {"//images/benchmarks:http_payload_" + str(size) for size in (1, 10, 100, 1024, 10240)}
    for cpu in ("k8", "aarch64"):
        for label in sorted(labels):
            selected = [row for row in rows if row["label"].removeprefix("@@") == label
                        and row["cpu"] == cpu and row["is_exec"] is False]
            match selected:
                case [row] if len(row["files"]) == 1:
                    pass
                case _:
                    raise ValueError(f"Expected one configured fixture file: {label} {cpu}: {selected}")
    for label in targets:
        selected = [row for row in rows if row["label"].removeprefix("@@") == label]
        match selected:
            case [row] if len(row["files"]) == 2:
                pass
            case _:
                raise ValueError(f"Expected two architecture outputs: {label}: {selected}")
