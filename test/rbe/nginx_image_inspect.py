"""Authenticate inherited Nginx layers and inspect the added netstat fixture."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time

from image_archive import archive, identity

os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
evidence = Path(sys.argv[1])
config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
graph = json.loads((evidence / "image-configured-inputs.json").read_text())
proof = json.loads((evidence / "mixed-images-image-execution.json").read_text())
root = Path(graph["execution_root"])
output_root = Path(graph["output_root"])
records = {"source": config["source"], "status": "INCOMPLETE", "architectures": [], "commands": [], "aggregate_outputs": []}


def run(arguments: list[str]) -> str:
    remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Nginx inspection exhausted the work deadline")
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=remaining)
    records["commands"].append({"arguments": arguments, "native_exit": result.returncode})
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    result.check_returncode()
    return result.stdout


def owner(label: str, cpu: str) -> dict:
    rows = [item for item in graph["rows"] if item["label"].removeprefix("@@") == label and item["cpu"] == cpu]
    match rows:
        case [row]:
            return row
        case _:
            raise ValueError(f"Expected one configured owner {label} {cpu}: {rows}")


def local_path(name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or any(part in ("..", ".") for part in path.parts) or path.parts[0] not in ("bazel-out", "external"):
        raise ValueError("Unexpected configured path: " + name)
    return (output_root if path.parts[0] == "external" else root) / path


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
            raise ValueError("Expected one successful producing action: " + relative)


try:
    if graph["source"] != config["source"] or proof["status"] != "PASS":
        raise ValueError("Mixed graph or native proof is not source-bound PASS")
    # Bind aggregate BEP paths to the original archive/context owner bytes.
    for image in ("nginx",):
        for kind in ("archive", "context"):
            aggregate = owner("//images/basic:qualification_" + image + "_" + kind + "s", "k8")
            expected_names = {"qualification_" + image + "_" + kind + "_" + arch + ".tar" for arch in ("amd64", "arm64")}
            if {Path(name).name for name in aggregate["files"]} != expected_names or len(aggregate["files"]) != 2:
                raise ValueError("Mixed aggregate output set differs")
            for arch, cpu in (("amd64", "k8"), ("arm64", "aarch64")):
                copied = local_path(next(name for name in aggregate["files"] if Path(name).name.endswith("_" + arch + ".tar")))
                original = output("//images/basic:" + image + "_" + kind, cpu)
                a, b = identity(copied), identity(original)
                if (a["sha256"], a["bytes"]) != (b["sha256"], b["bytes"]):
                    raise ValueError("Architecture aggregate differs from configured source output")
                records["aggregate_outputs"].append({"architecture": arch, "image": image, "kind": kind,
                    "aggregate_owner": aggregate["label"], "copy": producer(copied), "original": producer(original)})
    upstream_directory = Path(__file__).with_name("nginx_upstream")
    index_path = upstream_directory / "index.json"
    index_identity = identity(index_path)
    if "sha256:" + index_identity["sha256"] != config["upstream_index_digest"]:
        raise ValueError("Upstream index differs from the declared pin")
    upstream_index = json.loads(index_path.read_text())
    for architecture, cpu, machine in (("amd64", "k8", "Advanced Micro Devices X86-64"), ("arm64", "aarch64", "AArch64")):
        row = {"architecture": architecture, "status": "INCOMPLETE"}
        records["architectures"].append(row)
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
        inherited_layers = [
            {"sha256": layer["digest"].removeprefix("sha256:"), "bytes": layer["size"], "diff_id": diff_id}
            for layer, diff_id in zip(upstream_manifest["layers"], upstream_config["rootfs"]["diff_ids"], strict=True)
        ]
        row["upstream"] = {"index": index_identity, "manifest": manifest_identity, "config": config_identity,
                           "platform": descriptor["platform"], "config_values": upstream_config["config"]}
        metadata, files, elves = archive(output("//images/basic:nginx_archive", cpu), row, inherited_layers)
        if metadata["config"] != upstream_config["config"]:
            raise ValueError("Image did not inherit the exact upstream runtime configuration")
        if len(row["layers"]) != len(inherited_layers) + 2:
            raise ValueError("Image omitted or added an unexpected fixture layer")
        inventory = evidence / (architecture + "-nginx-inventory.json")
        context = output("//images/basic:nginx_context", cpu)
        row["context"] = identity(context)
        with tarfile.open(context) as contents:
            match [member for member in contents if member.isfile()]:
                case [member] if member.name.removeprefix("./") == "images/basic/nginx/image.tar":
                    with contents.extractfile(member) as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    if digest != row["archive"]["sha256"]:
                        raise ValueError("Make context differs from the image archive")
                case _:
                    raise ValueError("Make context must contain the sole expected image archive")
        if (metadata["os"], metadata["architecture"]) != ("linux", architecture):
            raise ValueError("Nginx target architecture differs")
        expected_files = {"usr/local/bin/netstat", "usr/share/licenses/busybox/LICENSE",
                          "usr/share/licenses/busybox/NOTICE", "usr/share/licenses/musl/NOTICE"}
        if {name for name, item in files.items() if item["type"] != "5"} != expected_files:
            raise ValueError("Fixture layer adds unexpected files or links")
        if set(elves) != {"usr/local/bin/netstat"}:
            raise ValueError("Fixture layer does not contain only the declared netstat executable")
        if any(item["type"] == "5" and item["mode"] != 0o755 for item in files.values()):
            raise ValueError("Fixture layer changed a directory's permissions")
        installed = "usr/local/bin/netstat"
        compiled = output("//images:busybox_binary", cpu)
        item = identity(compiled)
        if (files[installed]["mode"], files[installed]["sha256"], files[installed]["bytes"]) != (0o755, item["sha256"], item["bytes"]):
            raise ValueError("Installed netstat differs from the configured BusyBox output")
        header = run(["readelf", "--file-header", str(compiled)])
        segments = run(["readelf", "--program-headers", str(compiled)])
        dynamic = run(["readelf", "--dynamic", str(compiled)])
        if machine not in header or "INTERP" in segments or "(NEEDED)" in dynamic:
            raise ValueError("Added netstat executable is not target-static")
        markers = (str(root).encode(), str(output_root).encode(), b"/buildbuddy-execroot/", b"/root/workspace/", b"/tmp/image-qualification-", b"/Users/")
        if any(marker in elves[installed] for marker in markers):
            raise ValueError("Added netstat contains a worker-specific build path")
        row["static_programs"] = [{"installed_path": installed, "owner": "//images:busybox_binary", "output": item,
                                  "producer": producer(compiled), "file_header": header, "program_headers": segments, "dynamic": dynamic}]
        for installed, source in {
                "usr/share/licenses/busybox/NOTICE": Path("images/busybox/NOTICE"),
                "usr/share/licenses/musl/NOTICE": Path("images/musl/NOTICE")}.items():
            if (files[installed]["mode"], files[installed]["sha256"]) != (0o644, identity(source)["sha256"]):
                raise ValueError("Added notice differs: " + installed)
        if (files["usr/share/licenses/busybox/LICENSE"]["mode"], files["usr/share/licenses/busybox/LICENSE"]["sha256"]) != (
                0o644, "bbfc9843646d483c334664f651c208b9839626891d8f17604db2146962f43548"):
            raise ValueError("BusyBox license differs from its pinned source")
        configured = next(item for item in json.loads((evidence / "busybox-build-inputs.json").read_text())["architectures"]
                          if item["architecture"] == ("x86_64" if cpu == "k8" else "aarch64"))
        actual_config = Path(configured["outputs"]["config"][0]["retained"]).read_text().splitlines()
        required = {line for line in Path("images/busybox/applets.config").read_text().splitlines() if line.startswith("CONFIG_")}
        if not required <= set(actual_config):
            raise ValueError("Resolved BusyBox configuration dropped required applets")
        inventory.write_text(json.dumps(row, indent=2) + "\n")
        row["runtime_inputs"] = {"status": "PASS", "source": config["source"], "archive": row["archive"],
                                 "config_sha256": row["config_sha256"], "inventory": identity(inventory)}
        row["status"] = "PASS"
    records["status"] = "PASS"
finally:
    (evidence / "nginx-contents.json").write_text(json.dumps(records, indent=2) + "\n")
