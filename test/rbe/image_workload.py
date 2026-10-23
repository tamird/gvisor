"""Build both Redis image platforms and exercise the existing port-forward consumer."""
import json
import os
import shlex
from pathlib import Path


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    options = ["--compilation_mode=opt", "--config=x86_64", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc",
               "--build_event_json_file_path_conversion=false"]
    scripts = Path(__file__).parent
    result = invoke("canonical-execution-root", ["info", *options, "execution_root"], "command", targets=[])
    if result["status"] != "PASS":
        raise RuntimeError("Cannot identify the canonical execution root")
    execution_root = Path((evidence / "invocation-canonical-execution-root/stdout.log").read_text().strip())
    if not execution_root.is_absolute() or not execution_root.is_dir():
        raise ValueError("Invalid canonical execution root")
    targets = config["targets"]
    query = 'filter("^(@@)?//images/basic:", deps(set(' + " ".join(targets) + ')))'
    queried = invoke("mixed-image-owners", ["cquery", *options, "--output=starlark",
                     "--starlark:file=" + str(scripts / "image_query.bzl"), query], "command", targets=[])
    if queried["status"] != "PASS":
        raise RuntimeError("Mixed Redis image owner query failed")
    rows = [json.loads(line) for line in (evidence / "invocation-mixed-image-owners/stdout.log").read_text().splitlines()]
    graph = {"source": config["source"], "execution_root": str(execution_root), "output_root": str(output_root), "rows": rows}
    (evidence / "image-configured-inputs.json").write_text(json.dumps(graph, indent=2) + "\n")
    for cpu in ("k8", "aarch64"):
        for kind in ("archive", "context"):
            label = "//images/basic:redis_" + kind
            selected = [row for row in rows if row["label"].removeprefix("@@") == label and row["cpu"] == cpu]
            if len(selected) != 1 or len(selected[0]["files"]) != 1:
                raise ValueError(f"Expected one configured Redis output: {label} {cpu}: {selected}")
    for label in targets:
        selected = [row for row in rows if row["label"].removeprefix("@@") == label]
        if len(selected) != 1 or len(selected[0]["files"]) != 2:
            raise ValueError(f"Expected two architecture outputs: {label}: {selected}")
    if config.get("graph_only", False):
        return
    formatter = shlex.join(["bash", "-c", 'set -eu; export PATH="$(dirname "$1"):$PATH"; cd "$BUILD_WORKSPACE_DIRECTORY"; exec bash tools/lint.sh gofmt', "--"])
    formatted = invoke("gofmt", ["run", *options, *config["consumer_options"],
                       "--run_under=" + formatter, "@io_bazel_rules_go//go:go"], "command", targets=[])
    if formatted["status"] != "PASS":
        raise RuntimeError("Canonical Go formatting failed")
    owner = "//test/root:portforward_test_nogo"
    analyzed = invoke("redis-nogo", ["test", *options, *config["consumer_options"],
                      "--nocache_test_results", "--test_output=all", owner], "test", targets=[owner])
    if analyzed["status"] != "PASS" or analyzed["case_counts"] != {"failed": 0, "passed": 1, "skipped": 0}:
        raise RuntimeError("Port-forward owning analyzer failed")
    execution_log = private / "mixed-images-execution.json"
    try:
        built = invoke("mixed-images", ["build", *options, "--keep_going",
                       "--execution_log_json_file=" + str(execution_log), "--noexecution_log_sort", *targets],
                       "build", targets=targets)
    finally:
        record_execution(execution_log, "mixed", "images")
    if built["status"] != "PASS":
        raise RuntimeError("Mixed Redis image build failed")
    inspected = invoke("redis-image-closure", ["run", "--lockfile_mode=error", "--noannounce_rc",
                       "@rules_python//python/bin:python", "--", str(scripts / "redis_image_inspect.py"), str(evidence)],
                       "command", targets=[])
    if inspected["status"] != "PASS":
        raise RuntimeError("Pinned Redis base/script layer inspection failed")
    import redis_image_runtime
    redis_image_runtime.run(invoke, evidence, private)
