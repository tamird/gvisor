"""Build both image architectures together and exercise native profiling utilities."""
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
    programs = ["pidfd_test", "pidfd_fdinfo_test", "pidfd_open_test", "pidfd_poll_test",
                "pidfd_wait", "pidfd_getfd_test", "pidfd_setns_test"]
    sources = ["tools/testing/selftests/pidfd/" + name for name in
               [name + ".c" for name in programs] + ["Makefile", "config", "pidfd.h"]]
    sources += ["tools/testing/selftests/run_kselftest.sh", "tools/testing/selftests/kselftest/runner.sh",
                "tools/testing/selftests/kselftest/prefix.pl", "COPYING", "LICENSES/preferred/GPL-2.0",
                "LICENSES/exceptions/Linux-syscall-note", "Documentation/process/license-rules.rst"]
    query = ('filter("^(@@)?//images(:|/basic:|/benchmarks:)", deps(set(' + " ".join(targets) + '))) union set(' +
             " ".join("@linux_pidfd//:" + name for name in sources) +
             ')')
    queried = invoke("mixed-image-owners", ["cquery", *options, "--output=starlark",
                     "--starlark:file=" + str(scripts / "image_query.bzl"), query], "command", targets=[])
    if queried["status"] != "PASS":
        raise RuntimeError("Mixed image owner query failed")
    rows = [json.loads(line) for line in (evidence / "invocation-mixed-image-owners/stdout.log").read_text().splitlines()]
    graph = {"source": config["source"], "execution_root": str(execution_root), "output_root": str(output_root), "rows": rows}
    (evidence / "image-configured-inputs.json").write_text(json.dumps(graph, indent=2) + "\n")
    # Validate the new serializer and every required configured owner before
    # starting compilation. The files need not exist during this analysis.
    labels = ["//images/basic:" + name for name in programs + ["pidfd_test_list"]]
    labels += ["//images:busybox_binary", "//images:timeout_binary", "//images/benchmarks:profilehelper", "//images:chmod_binary", "//images:mkdir_binary", "//images:rm_binary"]
    labels += ["//images/" + package + ":" + image + "_" + kind
               for package, image in (("benchmarks", "profile_helper"), ("basic", "pidfd_tests"))
               for kind in ("archive", "context")]
    for cpu in ("k8", "aarch64"):
        for label in labels + ["//images:busybox_build", "//images:coreutils_build"]:
            selected = [item for item in rows if item["label"].removeprefix("@@") == label and item["cpu"] == cpu]
            if len(selected) != 1 or (label in labels and len(selected[0]["files"]) != 1):
                raise ValueError(f"Unexpected mixed owner shape: {label} {cpu}: {selected}")
            groups = selected[0]["groups"]
            expected = ({"Make_logs": 3, "busybox.config": 1} if label == "//images:busybox_build" else
                        {"Configure_logs": 3} if label == "//images:coreutils_build" else {})
            if any(len(groups.get(name, [])) != count for name, count in expected.items()):
                raise ValueError(f"Unexpected diagnostic groups: {label} {cpu}: {groups}")
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
    inspected = invoke("profile-image-closure", [*python, str(scripts / "profile_image_inspect.py"), str(evidence)],
                       "command", targets=[])
    if inspected["status"] != "PASS":
        raise RuntimeError("Profiling image/static/configure closure failed")
    contents = json.loads((evidence / "profile-contents.json").read_text())
    prior = config["pidfd_reference"]
    comparison = {"source": config["source"], "status": "INCOMPLETE", "reference": prior, "outputs": []}
    try:
        for architecture in ("amd64", "arm64"):
            match [row for row in contents["architectures"] if row["architecture"] == architecture]:
                case [row] if row["status"] == "PASS":
                    timeout = row["timeout"]["output"]
                case rows:
                    raise ValueError("Missing unique profiling architecture: " + str(rows))
            match [row for row in contents["aggregate_outputs"] if row["architecture"] == architecture
                   and row["image"] == "pidfd_tests" and row["kind"] == "archive"]:
                case [row]:
                    archive = row["original"]["output"]
                case rows:
                    raise ValueError("Missing unique PIDFD archive: " + str(rows))
            actual = {name: {field: value[field] for field in ("sha256", "bytes")}
                      for name, value in (("timeout", timeout), ("archive", archive))}
            comparison["outputs"].append({"architecture": architecture, "actual": actual,
                                           "expected": prior["outputs"][architecture],
                                           "equal": actual == prior["outputs"][architecture]})
        comparison["runtime_reused"] = all(row["equal"] for row in comparison["outputs"])
        if comparison["runtime_reused"]:
            comparison["status"] = "PASS"
        else:
            import image_pidfd_runtime
            image_pidfd_runtime.run(invoke, evidence, private)
            comparison["status"] = "PASS"
            comparison["new_runtime_result"] = identity(evidence / "pidfd-selftest-results.json")
    finally:
        (evidence / "profile-pidfd-applicability.json").write_text(json.dumps(comparison, indent=2) + "\n")
    tested = invoke("profile-native-host", [*python, str(scripts / "profile_image_runtime.py"),
                    str(evidence), str(private)], "command", targets=[])
    if tested["status"] != "PASS":
        raise RuntimeError("Native profiling command contract failed")
