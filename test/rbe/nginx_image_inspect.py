"""Authenticate the unchanged official Nginx benchmark image and Make contexts."""
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
records = {"source": config["source"], "status": "INCOMPLETE", "architectures": [], "aggregate_outputs": []}
package = "//images/benchmarks:"


def owner(label: str, cpu: str) -> dict:
    rows = [row for row in graph["rows"] if row["label"].removeprefix("@@") == label and row["cpu"] == cpu and row["is_exec"] is False]
    match rows:
        case [row]:
            return row
        case _:
            raise ValueError(f"Expected one configured owner {label} {cpu}: {rows}")


def local_path(name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("bazel-out",):
        raise ValueError("Unexpected configured output: " + name)
    return root / path


def output(label: str, cpu: str) -> Path:
    match owner(label, cpu)["files"]:
        case [name]:
            return local_path(name)
        case names:
            raise ValueError(f"Expected one configured file {label}: {names}")


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
            raise ValueError("Expected one successful native producer: " + relative)


try:
    if graph["source"] != config["source"] or proof["status"] != "PASS":
        raise ValueError("Mixed graph or native proof is not source-bound PASS")
    for kind in ("archive", "context"):
        aggregate = owner(package + "qualification_nginx_" + kind + "s", "k8")
        expected = {"qualification_nginx_" + kind + "_" + arch + ".tar" for arch in ("amd64", "arm64")}
        if {Path(name).name for name in aggregate["files"]} != expected or len(aggregate["files"]) != 2:
            raise ValueError("Mixed Nginx benchmark aggregate output set differs")
        for arch, cpu in (("amd64", "k8"), ("arm64", "aarch64")):
            copied = local_path(next(name for name in aggregate["files"] if Path(name).name.endswith("_" + arch + ".tar")))
            original = output(package + "nginx_" + kind, cpu)
            a, b = identity(copied), identity(original)
            if (a["sha256"], a["bytes"]) != (b["sha256"], b["bytes"]):
                raise ValueError("Aggregate differs from its configured source output")
            records["aggregate_outputs"].append({"architecture": arch, "kind": kind,
                "aggregate_owner": aggregate, "copy": producer(copied), "original": producer(original)})
    upstream = Path(__file__).with_name("nginx_benchmark_upstream")
    index_path = upstream / "index.json"
    if "sha256:" + identity(index_path)["sha256"] != config["upstream_index_digest"]:
        raise ValueError("Nginx benchmark index differs from the declared pin")
    index = json.loads(index_path.read_text())
    for arch, cpu in (("amd64", "k8"), ("arm64", "aarch64")):
        row = {"architecture": arch, "status": "INCOMPLETE"}
        records["architectures"].append(row)
        selected = [item for item in index["manifests"]
                    if item["platform"]["os"] == "linux" and item["platform"]["architecture"] == arch]
        match selected:
            case [descriptor]:
                pass
            case _:
                raise ValueError("Pinned index must select exactly one target platform")
        manifest_path = upstream / (arch + "-manifest.json")
        manifest_id = identity(manifest_path)
        if ("sha256:" + manifest_id["sha256"], manifest_id["bytes"]) != (descriptor["digest"], descriptor["size"]):
            raise ValueError("Nginx benchmark child manifest differs from its index")
        manifest = json.loads(manifest_path.read_text())
        config_path = upstream / (arch + "-config.json")
        config_id = identity(config_path)
        if ("sha256:" + config_id["sha256"], config_id["bytes"]) != (manifest["config"]["digest"], manifest["config"]["size"]):
            raise ValueError("Nginx benchmark config differs from its manifest")
        upstream_config = json.loads(config_path.read_text())
        inherited = [{"sha256": layer["digest"].removeprefix("sha256:"), "bytes": layer["size"], "diff_id": diff_id}
                     for layer, diff_id in zip(manifest["layers"], upstream_config["rootfs"]["diff_ids"], strict=True)]
        row["upstream"] = {"index": identity(index_path), "manifest": manifest_id, "config": config_id,
                           "platform": descriptor["platform"], "config_values": upstream_config["config"]}
        metadata, files, elves = archive(output(package + "nginx_archive", cpu), row, inherited)
        # rules_img preserves OCI runtime fields, not Docker's deprecated ArgsEscaped.
        for key in ("User", "ExposedPorts", "Env", "Entrypoint", "Cmd", "Volumes", "WorkingDir", "Labels", "StopSignal"):
            if (metadata["config"].get(key) or None) != (upstream_config["config"].get(key) or None):
                raise ValueError("Nginx benchmark runtime configuration changed: " + key)
        if (metadata["os"], metadata["architecture"]) != ("linux", arch) or len(row["layers"]) != len(inherited) + 2:
            raise ValueError("Nginx benchmark platform or inherited-layer count differs")
        expected_files = {"local/index.html", "etc/nginx/nginx.conf", "etc/nginx/nginx_gofer.conf"}
        row["fixture_configs"] = {}
        for name in ("nginx.conf", "nginx_gofer.conf"):
            source = identity(Path("images/benchmarks/nginx") / name)
            item = files["etc/nginx/" + name]
            if (item["type"], item["mode"], item["uid"], item["gid"], item["bytes"], item["sha256"]) != (
                    "0", 0o644, 0, 0, source["bytes"], source["sha256"]):
                raise ValueError("Nginx fixture config differs from source: " + name)
            row["fixture_configs"][name] = source
        row["payloads"] = []
        for size in (0, 1, 10, 100, 1024, 10240):
            installed = "local/latin" + str(size) + "k.txt" if size else "local/index.html"
            label = package + ("http_payload_" + str(size) if size else "nginx_index")
            expected_files.add(installed)
            expected_hash = hashlib.sha256(bytes(size * 1024)).hexdigest()
            item = files[installed]
            if (item["type"], item["mode"], item["uid"], item["gid"], item["bytes"], item["sha256"]) != (
                    "0", 0o644, 0, 0, size * 1024, expected_hash):
                raise ValueError("Nginx fixture payload differs: " + installed)
            payload = identity(output(label, cpu))
            if (payload["bytes"], payload["sha256"]) != (size * 1024, expected_hash):
                raise ValueError("Payload differs from declared write_file output: " + label)
            row["payloads"].append({"installed": installed, "owner": owner(label, cpu), "output": payload})
        if {name for name, item in files.items() if item["type"] != "5"} != expected_files or elves:
            raise ValueError("Nginx fixture contains unexpected files, links or programs")
        if any(item["type"] == "5" and item["mode"] != 0o755 for item in files.values()):
            raise ValueError("Nginx fixture directory permissions differ")
        context = output(package + "nginx_context", cpu)
        row["context"] = identity(context)
        with tarfile.open(context) as contents:
            match [member for member in contents if member.isfile()]:
                case [member] if member.name.removeprefix("./") == "images/benchmarks/nginx/image.tar":
                    with contents.extractfile(member) as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    if digest != row["archive"]["sha256"] or member.size != row["archive"]["bytes"]:
                        raise ValueError("Nginx benchmark Make context differs from its image archive")
                case _:
                    raise ValueError("Nginx benchmark Make context must contain only its expected archive")
        inventory = evidence / (arch + "-nginx-inventory.json")
        inventory.write_text(json.dumps(row, indent=2) + "\n")
        row["runtime_inputs"] = {"status": "PASS", "source": config["source"], "archive": row["archive"],
                                 "config_sha256": row["config_sha256"], "inventory": identity(inventory)}
        row["status"] = "PASS"
    records["status"] = "PASS"
finally:
    (evidence / "nginx-contents.json").write_text(json.dumps(records, indent=2) + "\n")
