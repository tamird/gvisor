"""Verify the native ARM64 pause executable and every descriptor in its archive."""

import hashlib
import io
import json
import platform
import struct
import tarfile
from pathlib import Path


def check(path):
    assert platform.machine() == "aarch64", platform.machine()
    image = "gvisor.dev/images/cri/pause:latest"
    manifest_count = 0
    payloads = []
    visited = set()
    with tarfile.open(path) as archive:
        docker_manifest = json.load(archive.extractfile("manifest.json"))
        assert [name for member in docker_manifest for name in member["RepoTags"]] == [image]
        queue = json.load(archive.extractfile("index.json"))["manifests"]
        while queue:
            descriptor = queue.pop()
            digest = descriptor["digest"]
            if digest in visited:
                continue
            visited.add(digest)
            blob = archive.extractfile("blobs/" + digest.replace(":", "/")).read()
            assert len(blob) == descriptor["size"] and "sha256:" + hashlib.sha256(blob).hexdigest() == digest, digest
            media = descriptor["mediaType"]
            if "index" in media or "manifest.list" in media:
                queue.extend(json.loads(blob)["manifests"])
            elif "manifest" in media:
                manifest_count += 1
                manifest = json.loads(blob)
                queue.extend([manifest["config"], *manifest["layers"]])
            elif "config" in media:
                config = json.loads(blob)
                assert (config["os"], config["architecture"]) == ("linux", "arm64")
                assert config["config"]["Entrypoint"] == ["/pause"]
            elif "layer" in media:
                with tarfile.open(fileobj=io.BytesIO(blob)) as layer:
                    for member in layer:
                        if member.name.removeprefix("./").removeprefix("/") == "pause":
                            assert member.isfile()
                            head = layer.extractfile(member).read(64)
                            assert head.startswith(b"\x7fELF\x02\x01")
                            machine, = struct.unpack_from("<H", head, 18)
                            assert machine == 183, machine  # EM_AARCH64
                            payloads.append(member.size)
            else:
                raise ValueError(media)
    assert manifest_count == 1 and len(payloads) == 1
    return dict(target="//test/root:sandbox_image_tar", image=image, os=config["os"],
                architecture=config["architecture"], worker=platform.machine(), descriptors_verified=len(visited),
                entrypoint="/pause", elf_machine=machine, executable_bytes=payloads[0],
                archive_sha256=hashlib.sha256(path.read_bytes()).hexdigest())


result = check(Path("bazel-bin/test/root/pause.tar"))
Path("pause-arm64.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
