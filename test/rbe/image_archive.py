"""Inspect image layers without extracting archive paths on the worker."""
import hashlib
import io
import json
import posixpath
from pathlib import Path
import re
import tarfile
import tempfile

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
    """Follow archive components in order with Linux's 40-symlink limit."""
    remaining = path.split('/')
    resolved = []
    links = 0
    while remaining:
        component = remaining.pop(0)
        if component in ('', '.'):
            continue
        if component == '..':
            if not resolved:
                raise RuntimeError('Image path escapes root: ' + path)
            resolved.pop()
            continue
        candidate = '/'.join([*resolved, component])
        member = files.get(candidate)
        if member is None:
            if not any(name.startswith(candidate + '/') for name in files):
                raise RuntimeError('Missing image path: ' + candidate)
            member = {'type': '5'}
        if member['type'] == '2':
            links += 1
            if links > 40:
                raise RuntimeError('Too many image symlinks: ' + path)
            target = member['link']
            if not target:
                raise RuntimeError('Empty image symlink target: ' + candidate)
            if target.startswith('/'):
                resolved = []
            remaining = target.split('/') + remaining
        else:
            if remaining and member['type'] != '5':
                raise RuntimeError('Image path component is not a directory: ' + candidate)
            resolved.append(component)
    result = '/'.join(resolved)
    if result not in files and not any(name.startswith(result + '/') for name in files):
        raise RuntimeError('Missing image path: ' + result)
    return result


def elf_closure(files, elf_files, machine, default_directories, run, destination):
    """Resolve each ELF dependency through its actual installed lookup paths."""
    result = {"dependencies": {}, "sonames": {}, "interpreters": {},
              "search_paths": {}, "resolved_dependencies": {}}
    reports = []
    with tempfile.TemporaryDirectory(prefix="image-elf-") as temporary:
        for index, (name, data) in enumerate(sorted(elf_files.items())):
            path = Path(temporary) / str(index)
            path.write_bytes(data)
            text = run(["readelf", "--file-header", "--program-headers", "--dynamic", str(path)])
            reports.append(name + "\n" + text)
            destination.write_text("\n".join(reports))
            if not re.search(r"^\s*Machine:\s*" + re.escape(machine) + r"\s*$", text, re.MULTILINE):
                raise ValueError("Wrong packaged ELF architecture: " + name)
            if re.search(r"\(FLAGS_1\).*\bNODEFLIB\b", text):
                raise ValueError("Unsupported NODEFLIB loader flag: " + name)
            result["dependencies"][name] = re.findall(r"\(NEEDED\).*?\[(.*?)\]", text)
            result["sonames"][name] = re.findall(r"\(SONAME\).*?\[(.*?)\]", text)
            # RUNPATH takes precedence over RPATH. Requiring every object to
            # resolve its own dependencies avoids assuming inherited RPATH or
            # a cache generated by installation scripts that never ran.
            paths = re.findall(r"\(RUNPATH\).*?\[(.*?)\]", text)
            if not paths:
                paths = re.findall(r"\(RPATH\).*?\[(.*?)\]", text)
            search = []
            for value in paths:
                for entry in value.split(":"):
                    expanded = re.sub(r"\$(?:\{ORIGIN\}|ORIGIN(?=/|$))",
                                      lambda _: "/" + posixpath.dirname(name), entry)
                    if not expanded.startswith("/") or "$" in expanded:
                        raise ValueError(f"Unsupported loader search path for {name}: {entry}")
                    search.append(expanded)
            result["search_paths"][name] = [*search, *default_directories]
            for loader in re.findall(r"Requesting program interpreter: (.*?)\]", text):
                resolved = resolve(files, loader)
                if resolved not in elf_files:
                    raise ValueError("Interpreter does not resolve to packaged ELF: " + loader)
                result["interpreters"][loader] = resolved
    try:
        for name, needed in result["dependencies"].items():
            resolved_dependencies = {}
            result["resolved_dependencies"][name] = resolved_dependencies
            for dependency in needed:
                if "/" in dependency:
                    if not dependency.startswith("/") or "$" in dependency:
                        raise ValueError(f"Unsupported direct dependency for {name}: {dependency}")
                    candidates = [dependency]
                else:
                    candidates = [posixpath.join(directory, dependency)
                                  for directory in result["search_paths"][name]]
                attempt = {"searched": candidates, "resolved": None}
                resolved_dependencies[dependency] = attempt
                for candidate in candidates:
                    try:
                        resolved = resolve(files, candidate)
                    except RuntimeError as error:
                        if str(error).startswith("Missing image path: "):
                            continue
                        raise
                    if resolved not in elf_files:
                        raise ValueError(f"Loader path is not ELF: {name}: {candidate}")
                    attempt.update(path=candidate, resolved=resolved, sha256=files[resolved]["sha256"])
                    break
                else:
                    raise ValueError(f"Unresolved installed dependency for {name}: {dependency}; {candidates}")
        if not result["interpreters"]:
            raise ValueError("Expected a declared dynamic package runtime")
    finally:
        destination.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
    return result
