"""Build both benchmark image architectures, then run existing Nginx and Empty consumers."""
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
    source_labels = ["@nginx_source//:" + name for name in ("LICENSE", "conf/mime.types", "html/index.html")]
    query = ('filter("^(@@)?//images(:|/basic:|/benchmarks:|/httpd:|/nginx:)", deps(set(' +
             " ".join(targets) + '))) union set(' + " ".join(source_labels) + ')')
    queried = invoke("mixed-image-owners", ["cquery", *options, "--output=starlark",
                     "--starlark:file=" + str(scripts / "image_query.bzl"), query], "command", targets=[])
    if queried["status"] != "PASS":
        raise RuntimeError("Mixed image owner query failed")
    rows = [json.loads(line) for line in (evidence / "invocation-mixed-image-owners/stdout.log").read_text().splitlines()]
    graph = {"source": config["source"], "execution_root": str(execution_root), "output_root": str(output_root), "rows": rows}
    (evidence / "image-configured-inputs.json").write_text(json.dumps(graph, indent=2) + "\n")
    # Check the selected transitions and every diagnostic group before builds.
    for cpu in ("k8", "aarch64"):
        labels = {"//images:busybox_binary": {}, "//images/httpd:binary": {}, "//images/nginx:binary": {},
                  "//images:busybox_build": {"Make_logs": 3, "busybox.config": 1},
                  "//images/nginx:nginx_build": {"Configure_logs": 3, "ngx_auto_config.h": 1}}
        labels.update({"//images/" + package + ":" + image + "_" + kind: {}
                       for package, image in (("basic", "nginx"), ("basic", "busybox"),
                                              ("benchmarks", "nginx"), ("benchmarks", "httpd"))
                       for kind in ("archive", "context")})
        labels.update({"//images/benchmarks:http_payload_" + str(size) + "k": {}
                       for size in (1, 10, 100, 1024, 10240)})
        for label, groups in labels.items():
            selected = [item for item in rows if item["label"].removeprefix("@@") == label
                        and item["cpu"] == cpu and item["is_exec"] is False]
            match selected:
                case [item] if item["files"]:
                    if any(len(item["groups"].get(name, [])) != count for name, count in groups.items()):
                        raise ValueError(f"Unexpected diagnostic group: {label} {cpu}: {item}")
                case _:
                    raise ValueError(f"Unexpected target owner: {label} {cpu}: {selected}")
    for label in targets:
        selected = [item for item in rows if item["label"].removeprefix("@@") == label]
        match selected:
            case [item] if len(item["files"]) == 2:
                pass
            case _:
                raise ValueError(f"Expected two split outputs: {label}: {selected}")
    execution_log = private / "mixed-images-execution.json"
    try:
        built = invoke("mixed-images", ["build", *options, "--keep_going",
                       "--execution_log_json_file=" + str(execution_log), "--noexecution_log_sort", *targets],
                       "build", targets=targets)
    finally:
        record_execution(execution_log, "mixed", "images")
    if built["status"] != "PASS":
        raise RuntimeError("Mixed benchmark image build failed")
    python = ["run", "--lockfile_mode=error", "--noannounce_rc", "@rules_python//python/bin:python", "--"]
    inspected = invoke("nginx-benchmark-image-closure", [*python, str(scripts / "nginx_image_inspect.py"), str(evidence)],
                       "command", targets=[])
    if inspected["status"] != "PASS":
        raise RuntimeError("Benchmark image/static/configure closure failed")
    import nginx_benchmark_runtime
    nginx_benchmark_runtime.run(invoke, evidence, private)
