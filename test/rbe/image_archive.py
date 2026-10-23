"""Inspect image layers without extracting archive paths on the worker."""
import hashlib
import io
import json
import posixpath
import tarfile

def identity(path):
    data = path.read_bytes()
    return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def archive(path, record, inherited_layers=()):
    """Inspect added files; authenticate opaque base layers by pinned identities."""
    files = {}
    elf_files = {}
    record["archive"] = identity(path)
    record["layers"] = []
    with tarfile.open(path) as outer:
        manifests = json.load(outer.extractfile("manifest.json"))
        if len(manifests) != 1:
            raise RuntimeError("Expected one image per archive")
        manifest = manifests[0]
        config_bytes = outer.extractfile(manifest["Config"]).read()
        config = json.loads(config_bytes)
        record["config"] = config
        record["config_sha256"] = hashlib.sha256(config_bytes).hexdigest()
        if len(inherited_layers) > len(manifest["Layers"]):
            raise ValueError("Image omitted inherited layers")
        record["inherited_layer_count"] = len(inherited_layers)
        record["files_scope"] = "appended layers" if inherited_layers else "entire image"
        for index, (layer, diff_id) in enumerate(zip(manifest["Layers"], config["rootfs"]["diff_ids"], strict=True)):
            payload = outer.extractfile(layer).read()
            item = {"name": layer, "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(), "expected_diff_id": diff_id}
            record["layers"].append(item)
            if layer != "blobs/sha256/" + item["sha256"]:
                raise RuntimeError("Stored layer differs from its blob identity")
            if index < len(inherited_layers):
                expected = inherited_layers[index]
                if (item["sha256"], item["bytes"], diff_id) != (expected["sha256"], expected["bytes"], expected["diff_id"]):
                    raise ValueError("Inherited layer differs from its pinned upstream identity")
                item["inherited"] = True
                # The upstream config supplies the diff-ID. Do not present it
                # as a fresh decompression or inspect upstream package contents.
                continue
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as encoded:
                encoded.fileobj.seek(0)
                uncompressed = encoded.fileobj.read()
            item["uncompressed_sha256"] = hashlib.sha256(uncompressed).hexdigest()
            if diff_id != "sha256:" + item["uncompressed_sha256"]:
                raise RuntimeError("Layer differs from its uncompressed config identity")
            with tarfile.open(fileobj=io.BytesIO(uncompressed), mode="r:") as contents:
                for member in contents:
                    name = member.name.removeprefix("./").lstrip("/").rstrip("/")
                    if not name:
                        continue
                    if ".." in name.split("/") or name.split("/")[-1].startswith(".wh."):
                        raise RuntimeError("Unexpected archive member: " + name)
                    data = contents.extractfile(member).read() if member.isfile() or member.islnk() else b""
                    elf_files.pop(name, None)
                    if data.startswith(b"\x7fELF"):
                        elf_files[name] = data
                    files[name] = {"type": member.type.decode(), "mode": member.mode,
                                   "uid": member.uid, "gid": member.gid, "link": member.linkname,
                                   "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    record["files"] = files
    return config, files, elf_files


def resolve(files, path):
    """Resolve only archive symlinks, without extracting to the host filesystem."""
    path = path.lstrip('/')
    seen = set()
    while True:
        if path in seen:
            raise RuntimeError('Cyclic image link: ' + path)
        seen.add(path)
        components = path.split('/')
        changed = False
        for index in range(len(components)):
            prefix = '/'.join(components[:index + 1])
            if prefix not in files or files[prefix]['type'] != '2':
                continue
            link = files[prefix]['link']
            target = link.lstrip('/') if link.startswith('/') else posixpath.join(posixpath.dirname(prefix), link)
            path = posixpath.normpath(posixpath.join(target, *components[index + 1:]))
            if path == '..' or path.startswith('../'):
                raise RuntimeError('Image link escapes root: ' + prefix)
            changed = True
            break
        if not changed:
            if path not in files:
                raise RuntimeError('Missing image path: ' + path)
            return path


def executable_aliases(files, elf_paths, expected):
    """Require canonical executables and verify any extra ELF hardlinks."""
    if set(expected) - set(elf_paths):
        raise ValueError("Scratch image is missing an expected ELF payload")
    aliases = {}
    for installed in sorted(set(elf_paths) - set(expected)):
        item = files[installed]
        target = item["link"]
        if (item["type"] != "1" or target not in expected or
                (item["bytes"], item["sha256"]) != (files[target]["bytes"], files[target]["sha256"])):
            raise ValueError("Scratch image contains an unexpected ELF payload: " + installed)
        aliases[installed] = target
    return aliases


def configured_inputs(rows, local_labels, source_names):
    """Bind exact local owners and source rows selected by explicit repo labels."""
    expected = {label.split(":", 1)[1] for label in local_labels} | set(source_names)
    outputs = {}
    source_repo = None
    for label, name, paths in rows:
        if name not in expected or name in outputs or len(paths) != 1 or label.rsplit(":", 1)[-1] != name:
            raise ValueError(f"Unexpected configured output: {label}: {paths}")
        path = paths[0]
        parts = path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("Unexpected configured path: " + path)
        if name in source_names:
            repo, separator, target = label.partition("//:")
            if (not separator or not repo.startswith("@@") or not repo[2:]
                    or target != name or path != "external/" + repo[2:] + "/" + name
                    or source_repo not in (None, repo)):
                raise ValueError("Unexpected configured source owner: " + label)
            source_repo = repo
        elif label.removeprefix("@@") not in local_labels or parts[0] != "bazel-out":
            raise ValueError("Unexpected configured local owner: " + label)
        outputs[name] = path
    if set(outputs) != expected:
        raise ValueError("Configured image input/output set differs")
    return outputs
