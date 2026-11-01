"""Inventory the exact public worker image without running it on the coordinator."""

import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import urllib.request

REPO = "https://gcr.io/v2/distroless/static-debian13/"
NATIVE = "sha256:d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"


def read(path, digest):
    request = urllib.request.Request(REPO + path + "/" + digest,
                                     headers={"Accept": "application/vnd.oci.image.manifest.v1+json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(64 * 1024 * 1024 + 1)
    assert len(raw) <= 64 * 1024 * 1024 and digest == "sha256:" + hashlib.sha256(raw).hexdigest()
    return raw


def main():
    manifest = json.loads(read("manifests", NATIVE))
    config = json.loads(read("blobs", manifest["config"]["digest"]))
    assert config["os"] == "linux" and config["architecture"] == "amd64"
    entries = {}
    for layer in manifest["layers"]:
        raw = read("blobs", layer["digest"])
        assert len(raw) == layer["size"]
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
            for member in tar:
                path = member.name.removeprefix("./").rstrip("/")
                assert not path.startswith("/") and ".." not in Path(path).parts and ".wh." not in path
                if not path or path == ".":
                    continue
                prefix = tar.extractfile(member).read(4) if member.isfile() else b""
                entries[path] = (member.isfile(), member.isdir(), member.mode, prefix)
    temp = entries["tmp"]
    assert temp[1] and temp[2] == 0o1777 and not any(path.startswith("tmp/") for path in entries)
    elfs = sorted(path for path, (_, _, _, prefix) in entries.items() if prefix == b"\x7fELF")
    scripts = sorted(path for path, (_, _, _, prefix) in entries.items() if prefix.startswith(b"#!"))
    exec_files = sorted(path for path, (is_file, _, mode, _) in entries.items() if is_file and mode & 0o111)
    assert not elfs and scripts == ["etc/update-motd.d/10-uname"]
    assert all(path not in entries for path in ["usr/bin/sh", "usr/bin/bash", "usr/bin/env", "usr/bin/cc",
                                               "usr/bin/gcc", "usr/bin/clang"])
    record = {"manifest": NATIVE, "config": manifest["config"]["digest"], "layers": manifest["layers"],
              "architecture": "amd64", "entries": len(entries), "tmp_mode": oct(temp[2]), "tmp_children": [],
              "top_level": sorted({path.split("/")[0] for path in entries}),
              "elf_paths": elfs, "shebang_scripts": scripts, "executable_mode_regular_files": exec_files}
    destination = Path(os.environ["RUNNER_TEMP"]) / "plugin-evidence" / "image.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(record, indent=2) + "\n")


if __name__ == "__main__":
    main()
