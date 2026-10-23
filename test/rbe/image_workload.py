"""Build both Apache image architectures together, then run their consumers."""
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
    source_labels = ["@" + repo + "//:" + name for repo in ("apr_source", "apr_util_source", "httpd_source")
                     for name in ("LICENSE", "NOTICE")]
    source_labels += ["@httpd_source//:docs/conf/mime.types", "@httpd_source//:docs/docroot/index.html", "@zlib//:LICENSE"]
    query = ('filter("^(@@)?//images(:|/basic:|/benchmarks:|/httpd:)", deps(set(' +
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
        labels = {"//images:busybox_binary": {}, "//images/httpd:binary": {},
                  "//images:busybox_build": {"Make_logs": 3, "busybox.config": 1},
                  "//images/httpd:apr": {"Configure_logs": 3, "apr.h": 1, "apr_private.h": 1, "config.log": 1},
                  "//images/httpd:apr_util": {"Configure_logs": 3, "apu_config.h": 1, "config.log": 1},
                  "//images/httpd:httpd": {"Configure_logs": 3, "ap_config_auto.h": 1, "ap_config_layout.h": 1, "config.log": 1}}
        labels.update({"//images/" + package + ":httpd_" + kind: {}
                       for package in ("basic", "benchmarks") for kind in ("archive", "context")})
        for label, groups in labels.items():
            selected = [item for item in rows if item["label"].removeprefix("@@") == label
                        and item["cpu"] == cpu and item["is_exec"] is False]
            match selected:
                case [item] if item["files"]:
                    if any(len(item["groups"].get(name, [])) != count for name, count in groups.items()):
                        raise ValueError(f"Unexpected diagnostic group: {label} {cpu}: {item}")
                case _:
                    raise ValueError(f"Unexpected target owner: {label} {cpu}: {selected}")
    exec_tools = [item for item in rows if item["label"].removeprefix("@@") == "//images:busybox_binary"
                  and item["is_exec"] is True]
    match exec_tools:
        case [item] if item["cpu"] == "k8" and len(item["files"]) == 1:
            pass
        case _:
            raise ValueError(f"Expected one AMD64 execution BusyBox for DD: {exec_tools}")
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
        raise RuntimeError("Mixed Apache image build failed")
    python = ["run", "--lockfile_mode=error", "--noannounce_rc", "@rules_python//python/bin:python", "--"]
    inspected = invoke("httpd-image-closure", [*python, str(scripts / "httpd_image_inspect.py"), str(evidence)],
                       "command", targets=[])
    if inspected["status"] != "PASS":
        raise RuntimeError("Apache image/static/configure closure failed")
    import httpd_image_runtime
    httpd_image_runtime.run(invoke, evidence, private)
