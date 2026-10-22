"""Inspect image layers without extracting archive paths on the worker."""
import hashlib
import io
import json
import posixpath
import tarfile

def identity(path):
    data = path.read_bytes()
    return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def archive(path, record):
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
        for layer, diff_id in zip(manifest["Layers"], config["rootfs"]["diff_ids"], strict=True):
            payload = outer.extractfile(layer).read()
            item = {"name": layer, "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(), "expected_diff_id": diff_id}
            record["layers"].append(item)
            if layer != "blobs/sha256/" + item["sha256"]:
                raise RuntimeError("Stored layer differs from its blob identity")
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
