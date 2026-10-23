"""Build both PHP image architectures and inspect their declared fixtures."""
import json
import os
from pathlib import Path


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    options = ["--compilation_mode=opt", "--config=x86_64", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc",
               "--build_event_json_file_path_conversion=false"]
    scripts = Path(__file__).parent
    result = invoke("canonical-execution-root", ["info", *options, "execution_root"], "command", targets=[])
    if result["status"] != "PASS":
        raise RuntimeError("Cannot identify canonical execution root")
    execution_root = Path((evidence / "invocation-canonical-execution-root/stdout.log").read_text().strip())
    if not execution_root.is_absolute() or not execution_root.is_dir():
        raise ValueError("Invalid canonical execution root")
    targets = config["targets"]
    query = ('filter("^(@@)?//images/runtimes:|^(@@)?//test/runtimes/proctor:proctor_bin$|^@@[^/]*php_test_source//:fixtures$", deps(set('
             + " ".join(targets) + ')))')
    result = invoke("mixed-image-owners", ["cquery", *options, "--output=starlark",
                    "--starlark:file=" + str(scripts / "image_query.bzl"), query], "command", targets=[])
    if result["status"] != "PASS":
        raise RuntimeError("Mixed PHP image owner query failed")
    rows = [json.loads(line) for line in (evidence / "invocation-mixed-image-owners/stdout.log").read_text().splitlines()]
    graph = {"source": config["source"], "execution_root": str(execution_root),
             "output_root": str(output_root), "rows": rows}
    (evidence / "image-configured-inputs.json").write_text(json.dumps(graph, indent=2) + "\n")
    for cpu in ("k8", "aarch64"):
        for label in ("//images/runtimes:php_archive", "//images/runtimes:php_context"):
            selected = [row for row in rows if row["label"].removeprefix("@@") == label and row["cpu"] == cpu]
            if len(selected) != 1 or len(selected[0]["files"]) != 1:
                raise ValueError(f"Unexpected configured owner: {label} {cpu}: {selected}")
        selected = [row for row in rows if row["label"].endswith("php_test_source//:fixtures") and row["cpu"] == cpu]
        if len(selected) != 1 or len(selected[0]["files"]) != 1:
            raise ValueError(f"Unexpected PHP fixture owner: {cpu}: {selected}")
    for label in config["image_targets"]["x86_64"]:
        selected = [row for row in rows if row["label"].removeprefix("@@") == label]
        if len(selected) != 1 or len(selected[0]["files"]) != 2:
            raise ValueError(f"Expected two split outputs: {label}: {selected}")
    selected = [row for row in rows if row["label"].removeprefix("@@") == "//test/runtimes/proctor:proctor_bin" and row["cpu"] == "k8"]
    if len(selected) != 1 or len(selected[0]["files"]) != 1:
        raise ValueError(f"Expected exactly one configured proctor: {selected}")
    if config.get("graph_only", False):
        return
    execution_log = private / "mixed-images-execution.json"
    try:
        built = invoke("mixed-images", ["build", *options, "--keep_going",
                       "--execution_log_json_file=" + str(execution_log), "--noexecution_log_sort", *targets],
                       "build", targets=targets)
    finally:
        record_execution(execution_log, "mixed", "images")
    if built["status"] != "PASS":
        raise RuntimeError("Mixed PHP image build failed")
    python = ["run", "--lockfile_mode=error", "--noannounce_rc", "@rules_python//python/bin:python", "--"]
    result = invoke("php-image-closure", [*python, str(scripts / "php_image_inspect.py"), str(evidence)],
                    "command", targets=[])
    if result["status"] != "PASS":
        raise RuntimeError("Pinned PHP image/fixture closure failed")
    import php_image_runtime
    php_image_runtime.run(invoke, evidence, private)
