"""Build both image architectures together and run existing Nginx consumers."""
import json
import os
from pathlib import Path
import shutil

from image_archive import identity


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
    query = 'filter("^(@@)?//images(:|/basic:)", deps(set(' + " ".join(targets) + ')))'
    queried = invoke("mixed-image-owners", ["cquery", *options, "--output=starlark",
                     "--starlark:file=" + str(scripts / "image_query.bzl"), query], "command", targets=[])
    if queried["status"] != "PASS":
        raise RuntimeError("Mixed image owner query failed")
    rows = [json.loads(line) for line in (evidence / "invocation-mixed-image-owners/stdout.log").read_text().splitlines()]
    graph = {"source": config["source"], "execution_root": str(execution_root), "output_root": str(output_root), "rows": rows}
    (evidence / "image-configured-inputs.json").write_text(json.dumps(graph, indent=2) + "\n")
    # Validate the new serializer and every required configured owner before
    # starting compilation. The files need not exist during this analysis.
    labels = ["//images:busybox_binary"]
    labels += ["//images/basic:nginx_" + kind for kind in ("archive", "context")]
    for cpu in ("k8", "aarch64"):
        for label in labels + ["//images:busybox_build"]:
            selected = [item for item in rows if item["label"].removeprefix("@@") == label and item["cpu"] == cpu]
            if len(selected) != 1 or (label in labels and len(selected[0]["files"]) != 1):
                raise ValueError(f"Unexpected mixed owner shape: {label} {cpu}: {selected}")
            expected = {"Make_logs": 3, "busybox.config": 1} if label == "//images:busybox_build" else {}
            if any(len(selected[0]["groups"].get(name, [])) != count for name, count in expected.items()):
                raise ValueError(f"Unexpected diagnostic groups: {label} {cpu}: {selected}")
    for label in targets:
        selected = [item for item in rows if item["label"].removeprefix("@@") == label]
        if len(selected) != 1 or len(selected[0]["files"]) != 2:
            raise ValueError(f"Expected exactly two split outputs: {label}: {selected}")
    execution_log = private / "mixed-images-execution.json"
    try:
        built = invoke("mixed-images", ["build", *options, "--keep_going",
                       "--execution_log_json_file=" + str(execution_log), "--noexecution_log_sort", *targets],
                       "build", targets=targets)
    finally:
        record_execution(execution_log, "mixed", "images")
    if built["status"] != "PASS":
        raise RuntimeError("Mixed image build failed")
    records = {"source": config["source"], "status": "INCOMPLETE", "architectures": []}
    try:
        for architecture, cpu in (("x86_64", "k8"), ("aarch64", "aarch64")):
            row = {"architecture": architecture, "build_status": built["status"], "outputs": {}}
            records["architectures"].append(row)
            for name, owner, group, expected in (
                    ("binary", "//images:busybox_binary", None, 1),
                    ("config", "//images:busybox_build", "busybox.config", 1),
                    ("logs", "//images:busybox_build", "Make_logs", 3)):
                selected = [item for item in rows if item["label"].removeprefix("@@") == owner and item["cpu"] == cpu]
                match selected:
                    case [item]:
                        paths = item["groups"][group] if group else item["files"]
                    case _:
                        raise ValueError(f"Expected one configured owner: {architecture} {owner}: {selected}")
                if len(paths) != expected:
                    raise ValueError(f"Unexpected configured output count: {architecture} {name}: {paths}")
                retained = []
                for index, output in enumerate(paths):
                    path = Path(output)
                    if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("bazel-out",):
                        raise ValueError("Unexpected configured output: " + output)
                    actual = execution_root / path
                    item = {"output": str(actual), **identity(actual), "retained": None}
                    if name != "binary":
                        if actual.stat().st_size > 8 * 1024 * 1024:
                            raise ValueError("BusyBox diagnostic output exceeds 8 MiB")
                        destination = evidence / (architecture + "-busybox-" + name + "-" + str(index) + "-" + path.name)
                        shutil.copyfile(actual, destination)
                        item["retained"] = str(destination)
                    retained.append(item)
                row["outputs"][name] = retained
        records["status"] = "PASS"
    finally:
        (evidence / "busybox-build-inputs.json").write_text(json.dumps(records, indent=2) + "\n")
    python = ["run", "--lockfile_mode=error", "--noannounce_rc", "@rules_python//python/bin:python", "--"]
    inspected = invoke("nginx-image-closure", [*python, str(scripts / "nginx_image_inspect.py"), str(evidence)],
                       "command", targets=[])
    if inspected["status"] != "PASS":
        raise RuntimeError("Pinned Nginx image/layer closure failed")
    import nginx_image_runtime
    nginx_image_runtime.run(invoke, evidence, private)
