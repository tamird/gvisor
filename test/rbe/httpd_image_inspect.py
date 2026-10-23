"""Authenticate Apache base images and inspect the appended benchmark files."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tarfile

from image_archive import archive, identity

os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
evidence = Path(sys.argv[1])
config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
graph = json.loads((evidence / "image-configured-inputs.json").read_text())
proof = json.loads((evidence / "mixed-images-image-execution.json").read_text())
root = Path(graph["execution_root"])
records = {"status": "INCOMPLETE", "source": config["source"], "aggregate_outputs": []}


def owner(label: str, cpu: str) -> dict:
    selected = [item for item in graph["rows"] if item["label"].removeprefix("@@") == label
                and item["cpu"] == cpu and item["is_exec"] is False]
    match selected:
        case [item]:
            return item
        case _:
            raise ValueError(f"Expected one configured owner {label} {cpu}: {selected}")


def local_path(name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("bazel-out",):
        raise ValueError("Unexpected configured output path: " + name)
    result = root / path
    if not result.is_file():
        raise ValueError("Configured output is not materialized: " + str(result))
    return result


def output(label: str, cpu: str) -> Path:
    match owner(label, cpu)["files"]:
        case [name]:
            return local_path(name)
        case names:
            raise ValueError(f"Expected one output for {label}: {names}")


def producer(path: Path) -> dict:
    item = identity(path)
    relative = str(path.relative_to(root))
    matches = [action for action in proof["image_actions"] if any(
        out["path"] == relative and out.get("digest", {}).get("hash") == item["sha256"]
        and int(out.get("digest", {}).get("sizeBytes", -1)) == item["bytes"] for out in action["actualOutputs"])]
    match matches:
        case [action] if action["exitCode"] == 0 and not action["status"] and action["digest"]["hash"]:
            return {"output": item, "action": action}
        case _:
            raise ValueError("Expected one successful producing action: " + relative)


def runtime_config(value: dict) -> dict:
    # The legacy Docker config also contains builder/container state. Keep the
    # complete document in evidence, but compare the runtime image interface.
    fields = {"User", "Env", "Entrypoint", "Cmd", "WorkingDir", "ExposedPorts",
              "Volumes", "Labels", "StopSignal", "Healthcheck", "OnBuild"}
    return {key: value[key] for key in fields if value.get(key) not in (None, "", [], {})}


try:
    if graph["source"] != config["source"] or proof["status"] != "PASS":
        raise ValueError("Mixed graph/native proof is not exact-source PASS")
    for package in ("basic", "benchmarks"):
        document = {"status": "INCOMPLETE", "architectures": []}
        destination = evidence / (package + "-httpd-contents.json")
        upstream_directory = Path(__file__).with_name("httpd_upstream") / package
        index_path = upstream_directory / "index.json"
        index_identity = identity(index_path)
        if "sha256:" + index_identity["sha256"] != config["upstream_index_digests"][package]:
            raise ValueError("Upstream index differs from the declared pin")
        upstream_index = json.loads(index_path.read_text())
        try:
            for architecture, cpu in (("amd64", "k8"), ("arm64", "aarch64")):
                row = {"architecture": architecture, "status": "INCOMPLETE"}
                document["architectures"].append(row)
                for kind in ("archive", "context"):
                    aggregate = owner("//images/" + package + ":qualification_httpd_" + kind + "s", "k8")
                    expected_names = {"qualification_httpd_" + kind + "_" + arch + ".tar" for arch in ("amd64", "arm64")}
                    if len(aggregate["files"]) != 2 or {Path(name).name for name in aggregate["files"]} != expected_names:
                        raise ValueError("Mixed aggregate output set differs")
                    copied = local_path(next(name for name in aggregate["files"] if Path(name).name.endswith("_" + architecture + ".tar")))
                    original = output("//images/" + package + ":httpd_" + kind, cpu)
                    a, b = identity(copied), identity(original)
                    if (a["sha256"], a["bytes"]) != (b["sha256"], b["bytes"]):
                        raise ValueError("Aggregate bytes differ from the original configured output")
                    records["aggregate_outputs"].append({"package": package, "architecture": architecture, "kind": kind,
                        "owner": aggregate, "copy": producer(copied), "original": producer(original)})
                selected = [item for item in upstream_index["manifests"]
                            if item["platform"]["os"] == "linux" and item["platform"]["architecture"] == architecture]
                match selected:
                    case [descriptor]:
                        pass
                    case _:
                        raise ValueError("Pinned index does not select exactly one target image")
                manifest_path = upstream_directory / (architecture + "-manifest.json")
                manifest_identity = identity(manifest_path)
                if ("sha256:" + manifest_identity["sha256"], manifest_identity["bytes"]) != (descriptor["digest"], descriptor["size"]):
                    raise ValueError("Upstream manifest differs from the selected index entry")
                upstream_manifest = json.loads(manifest_path.read_text())
                config_path = upstream_directory / (architecture + "-config.json")
                config_identity = identity(config_path)
                if ("sha256:" + config_identity["sha256"], config_identity["bytes"]) != (
                        upstream_manifest["config"]["digest"], upstream_manifest["config"]["size"]):
                    raise ValueError("Upstream config differs from its manifest")
                upstream_config = json.loads(config_path.read_text())
                inherited = [{"sha256": layer["digest"].removeprefix("sha256:"), "bytes": layer["size"], "diff_id": diff_id}
                             for layer, diff_id in zip(upstream_manifest["layers"], upstream_config["rootfs"]["diff_ids"], strict=True)]
                row["upstream"] = {"index": index_identity, "manifest": manifest_identity, "config": config_identity,
                                   "platform": descriptor["platform"], "config_values": upstream_config["config"]}
                metadata, files, elves = archive(output("//images/" + package + ":httpd_archive", cpu), row, inherited)
                if runtime_config(metadata["config"]) != runtime_config(upstream_config["config"]):
                    raise ValueError("Apache did not inherit the upstream runtime configuration")
                row["runtime_config"] = runtime_config(metadata["config"])
                if len(row["layers"]) != len(inherited) + (2 if package == "benchmarks" else 0):
                    raise ValueError("Image omitted or added an unexpected fixture layer")
                if (metadata["os"], metadata["architecture"]) != ("linux", architecture):
                    raise ValueError("Apache target architecture differs")
                context = output("//images/" + package + ":httpd_context", cpu)
                row["context"] = identity(context)
                with tarfile.open(context) as contents:
                    match [member for member in contents if member.isfile()]:
                        case [member] if member.name.removeprefix("./") == "images/" + package + "/httpd/image.tar":
                            with contents.extractfile(member) as stream:
                                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                            if digest != row["archive"]["sha256"]:
                                raise ValueError("Make context differs from image archive")
                        case _:
                            raise ValueError("Unexpected Make context members")
                expected_files = set()
                row["payloads"] = []
                if package == "benchmarks":
                    expected_files = {"usr/local/apache2/conf/httpd.conf", "usr/local/bin/apache2"}
                    installed_config = files["usr/local/apache2/conf/httpd.conf"]
                    source_config = identity(Path("images/benchmarks/httpd/httpd.conf"))
                    if (installed_config["type"], installed_config["mode"], installed_config["sha256"]) != ("0", 0o644, source_config["sha256"]):
                        raise ValueError("Installed benchmark configuration differs from source")
                    alias = files["usr/local/bin/apache2"]
                    if (alias["type"], alias["link"]) != ("2", "/usr/local/apache2/bin/httpd"):
                        raise ValueError("Benchmark command alias differs")
                    row["fixture_config"] = source_config
                    for size in (1, 10, 100, 1024, 10240):
                        installed = "local/latin" + str(size) + "k.txt"
                        expected_files.add(installed)
                        expected_hash = hashlib.sha256(bytes(size * 1024)).hexdigest()
                        if (files[installed]["type"], files[installed]["mode"], files[installed]["bytes"], files[installed]["sha256"]) != ("0", 0o644, size * 1024, expected_hash):
                            raise ValueError("Benchmark payload size/content/mode differs")
                        label = "//images/benchmarks:http_payload_" + str(size)
                        payload = identity(output(label, cpu))
                        if (payload["bytes"], payload["sha256"]) != (size * 1024, expected_hash):
                            raise ValueError("Image payload differs from declared write_file output")
                        row["payloads"].append({"installed": installed, "owner": owner(label, cpu), "output": payload})
                if {name for name, item in files.items() if item["type"] != "5"} != expected_files or elves:
                    raise ValueError("Fixture layers contain unexpected files, links or executables")
                if any(item["type"] == "5" and item["mode"] != 0o755 for item in files.values()):
                    raise ValueError("Fixture layer changed a directory's permissions")
                row["runtime_inputs"] = {"status": "PASS", "source": config["source"], "archive": row["archive"],
                                         "config_sha256": row["config_sha256"]}
                row["status"] = "PASS"
            document["status"] = "PASS"
        finally:
            destination.write_text(json.dumps(document, indent=2) + "\n")
    records["status"] = "PASS"
finally:
    (evidence / "httpd-build-closure.json").write_text(json.dumps(records, indent=2) + "\n")
