"""Authenticate the PHP base and compare the complete appended source fixture."""
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
output_root = Path(graph["output_root"])
records = {"source": config["source"], "status": "INCOMPLETE", "architectures": [], "aggregate_outputs": []}


def owner(label: str, cpu: str) -> dict:
    rows = [row for row in graph["rows"] if row["label"].removeprefix("@@") == label and row["cpu"] == cpu]
    match rows:
        case [row]:
            return row
        case _:
            raise ValueError(f"Expected one configured owner {label} {cpu}: {rows}")


def local_path(name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "bazel-out":
        raise ValueError("Unexpected configured output: " + name)
    return root / path


def output(label: str, cpu: str) -> Path:
    match owner(label, cpu)["files"]:
        case [name]:
            return local_path(name)
        case names:
            raise ValueError(f"Expected one configured file {label}: {names}")


def producer(path: Path) -> dict:
    actual = identity(path)
    relative = str(path.relative_to(root))
    matches = [action for action in proof["image_actions"] if any(
        out["path"] == relative and out.get("digest", {}).get("hash") == actual["sha256"]
        and int(out.get("digest", {}).get("sizeBytes", -1)) == actual["bytes"] for out in action["actualOutputs"])]
    match matches:
        case [action] if action["exitCode"] == 0 and not action["status"] and action["digest"]["hash"]:
            return {"output": actual, "action": action}
        case _:
            raise ValueError("Expected one successful producing action: " + relative)


try:
    if graph["source"] != config["source"] or proof["status"] != "PASS":
        raise ValueError("Mixed graph or native proof is not source-bound PASS")
    proctor = output("//test/runtimes/proctor:proctor_bin", "k8")
    records["proctor"] = {"owner": "//test/runtimes/proctor:proctor_bin", "source": config["source"],
                          "producer": producer(proctor)}
    if records["proctor"]["producer"]["action"]["mnemonic"] != "GoLink":
        raise ValueError("Configured proctor has no exact GoLink producer")
    for kind in ("archive", "context"):
        aggregate = owner("//images/runtimes:qualification_php_" + kind + "s", "k8")
        expected = {"qualification_php_" + kind + "_" + arch + ".tar" for arch in ("amd64", "arm64")}
        if {Path(name).name for name in aggregate["files"]} != expected or len(aggregate["files"]) != 2:
            raise ValueError("Mixed aggregate output set differs")
        for arch, cpu in (("amd64", "k8"), ("arm64", "aarch64")):
            copied = local_path(next(name for name in aggregate["files"] if Path(name).name.endswith("_" + arch + ".tar")))
            original = output("//images/runtimes:php_" + kind, cpu)
            a, b = identity(copied), identity(original)
            if (a["sha256"], a["bytes"]) != (b["sha256"], b["bytes"]):
                raise ValueError("Aggregate differs from configured source output")
            records["aggregate_outputs"].append({"architecture": arch, "image": "php", "kind": kind,
                "aggregate_owner": aggregate["label"], "copy": producer(copied), "original": producer(original)})
    upstream_directory = Path(__file__).with_name("php_upstream")
    index_path = upstream_directory / "index.json"
    index_identity = identity(index_path)
    if "sha256:" + index_identity["sha256"] != config["upstream_index_digest"]:
        raise ValueError("Upstream index differs from the declared pin")
    upstream_index = json.loads(index_path.read_text())
    for architecture, cpu in (("amd64", "k8"), ("arm64", "aarch64")):
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
            raise ValueError("Upstream manifest differs from selected index entry")
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
        metadata, files, _ = archive(output("//images/runtimes:php_archive", cpu), row, inherited)
        expected_config = {**upstream_config["config"], "WorkingDir": "/root/php-8.5.11"}
        if metadata["config"] != expected_config or (metadata["os"], metadata["architecture"]) != ("linux", architecture):
            raise ValueError("PHP image configuration differs beyond the declared working directory")
        if len(row["layers"]) != len(inherited) + 1:
            raise ValueError("Image omitted or added an unexpected fixture layer")
        selected = [item for item in graph["rows"] if item["label"].endswith("php_test_source//:fixtures") and item["cpu"] == cpu]
        match selected:
            case [fixture] if fixture["label"].startswith("@@") and len(fixture["files"]) == 1:
                pass
            case _:
                raise ValueError("Missing or ambiguous configured PHP source fixture")
        repository, separator, target = fixture["label"][2:].partition("//:")
        if not separator or target != "fixtures" or "/" in repository or repository in ("", ".", ".."):
            raise ValueError("Malformed configured source repository")
        source_root = output_root / "external" / repository
        expected_files = {}
        excluded = {"BUILD.bazel", "REPO.bazel", "WORKSPACE", "WORKSPACE.bazel"}
        for directory, _, filenames in os.walk(source_root):
            for filename in filenames:
                source = Path(directory) / filename
                relative = source.relative_to(source_root).as_posix()
                if relative in excluded:
                    continue
                if source.is_symlink() or not source.is_file():
                    raise ValueError("Unexpected source fixture type: " + relative)
                actual = identity(source)
                expected_files["root/php-8.5.11/" + relative] = (
                    actual["sha256"], actual["bytes"], 0o755 if source.stat().st_mode & 0o111 else 0o644)
        actual_files = {name: (item["sha256"], item["bytes"], item["mode"])
                        for name, item in files.items() if item["type"] == "0"}
        if not expected_files or actual_files != expected_files or any(
                item["type"] not in ("0", "5") or (item["uid"], item["gid"]) != (0, 0)
                for item in files.values()):
            raise ValueError("Appended PHP source files differ from the configured patched repository")
        phpt = sorted(name.removeprefix("root/php-8.5.11/") for name in actual_files if name.endswith(".phpt"))
        if len(phpt) != 21872 or hashlib.sha256(("\n".join(phpt) + "\n").encode()).hexdigest() != config["phpt_names_sha256"]:
            raise ValueError("Complete PHPT name set differs from the pinned release")
        for relative, expected_hash in config["patched_phpt_sha256"].items():
            if actual_files["root/php-8.5.11/" + relative][0] != expected_hash:
                raise ValueError("Patched PHPT prerequisite differs: " + relative)
        row["fixture"] = {"owner": fixture["label"], "producer": producer(local_path(fixture["files"][0])),
                          "source_root": str(source_root), "file_count": len(actual_files), "phpt_count": len(phpt)}
        context = output("//images/runtimes:php_context", cpu)
        row["context"] = identity(context)
        with tarfile.open(context) as contents:
            match [member for member in contents if member.isfile()]:
                case [member] if member.name.removeprefix("./") == "images/runtimes/php8.5.11/image.tar":
                    with contents.extractfile(member) as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    if digest != row["archive"]["sha256"]:
                        raise ValueError("Make context differs from image archive")
                case _:
                    raise ValueError("Make context must contain the sole expected image archive")
        inventory = evidence / (architecture + "-php-inventory.json")
        inventory.write_text(json.dumps(row, indent=2) + "\n")
        row["runtime_inputs"] = {"status": "PASS", "source": config["source"], "archive": row["archive"],
                                 "config_sha256": row["config_sha256"], "inventory": identity(inventory)}
        row["status"] = "PASS"
    records["status"] = "PASS"
finally:
    (evidence / "php-contents.json").write_text(json.dumps(records, indent=2) + "\n")
