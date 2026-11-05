# Copyright 2026 The gVisor Authors.
# SPDX-License-Identifier: Apache-2.0
"""Verify the directory-only OCI image and one Bazel ObserveWorkerRoot action."""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import struct
import tarfile

OWNER = "//test/rbe/directory_worker:observe"
MARKER = "gvisor-worker-root"
ABSENT = {"/bin/sh", "/bin/bash", "/bin/busybox", "/usr/bin/env",
          "/usr/bin/cc", "/usr/bin/gcc", "/usr/bin/clang"}
MACHINES = {"amd64": (62, "x86_64"), "arm64": (183, "aarch64")}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inventory(layout):
    def read(desc):
        digest = desc["digest"]
        require(re.fullmatch(r"sha256:[a-f0-9]{64}", digest), "blob digest")
        path = layout / "blobs" / "sha256" / digest[7:]
        require(0 < path.stat().st_size == desc["size"] <= 1024 * 1024, "blob size")
        data = path.read_bytes()
        require("sha256:" + sha(data) == digest, "blob content")
        return data

    root = json.loads((layout / "index.json").read_bytes())
    result = {}
    descriptors = root["manifests"]
    if len(descriptors) == 1:
        descriptors = json.loads(read(descriptors[0]))["manifests"]
    require(len(descriptors) == 2, "architecture count")
    for desc in descriptors:
        manifest = json.loads(read(desc))
        config = json.loads(read(manifest["config"]))
        arch = config["architecture"]
        require(arch in MACHINES and arch not in result and config["os"] == "linux", "config platform")
        require(desc["platform"]["architecture"] == arch and desc["platform"]["os"] == "linux", "index platform")
        layer, = manifest["layers"]
        require(layer["mediaType"] == "application/vnd.oci.image.layer.v1.tar+gzip", "layer encoding")
        with gzip.GzipFile(fileobj=io.BytesIO(read(layer))) as stream:
            raw = stream.read(1024 * 1024 + 1)
        require(len(raw) <= 1024 * 1024 and config["rootfs"]["diff_ids"] == ["sha256:" + sha(raw)], "uncompressed layer")
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
            member, = tar.getmembers()
            require(member.isdir() and member.size == 0 and member.name in (MARKER, "./" + MARKER), "sole tar member")
            require(not raw[member.offset_data:].strip(b"\0"), "trailing tar data")
        result[arch] = dict(manifest=desc["digest"], layer=layer["digest"], diff_id=sha(raw),
                            members=1, directories=[MARKER], regular_files=0, links=0)
    require(len({entry["layer"] for entry in result.values()}) == 1, "shared physical layer")
    return result


def static_elf(data, arch):
    require(len(data) > 64 and data[:6] == b"\x7fELF\x02\x01", "ELF64/LE")
    kind, machine = struct.unpack_from("<HH", data, 16)
    require(kind in (2, 3) and machine == MACHINES[arch][0], "target executable")
    offset, = struct.unpack_from("<Q", data, 32)
    size, count = struct.unpack_from("<HH", data, 54)
    require(size == 56 and 0 < count < 256 and offset + size * count <= len(data), "program headers")
    for i in range(count):
        segment, _, start, _, _, length, _, _ = struct.unpack_from("<IIQQQQQQ", data, offset + size*i)
        require(segment != 3, "interpreter")
        if segment == 2:
            require(length % 16 == 0 and start + length <= len(data), "dynamic segment")
            require(all(tag != 1 for tag, _ in struct.iter_unpack("<qQ", data[start:start+length])), "needed library")


def action(args):
    arch = args.arch
    images = inventory(args.layout)
    image = "docker://" + args.repository + "@" + images[arch]["manifest"]
    props = {"Arch": arch, "OSFamily": "linux", "container-image": image,
             "workload-isolation-type": "oci", "recycle-runner": "false"}
    events = [json.loads(line) for line in args.bep.open()]
    starts = [row["started"]["uuid"] for row in events if "started" in row]
    require(starts == [args.invocation], "invocation identity")
    require(not any("testResult" in row["id"] for row in events), "unexpected TestRunner")
    completions = [row for row in events if row["id"].get("targetCompleted", {}).get("label") == OWNER
                   and not row["id"]["targetCompleted"].get("aspect")]
    require(len(completions) == 1 and completions[0].get("completed", {}).get("success") is True, "target completion")
    finish, = [row["finished"] for row in events if "buildFinished" in row["id"]]
    require(finish["exitCode"]["name"] == "SUCCESS", "build exit")
    require(args.log.stat().st_size <= 64 * 1024 * 1024, "execution log size")
    records = args.log.read_text()
    matches = []
    pos = 0
    decoder = json.JSONDecoder()
    while pos < len(records):
        if records[pos].isspace():
            pos += 1
            continue
        spawn, pos = decoder.raw_decode(records, pos)
        if spawn.get("mnemonic") == "ObserveWorkerRoot":
            matches.append(spawn)
    spawn, = matches
    require(spawn["targetLabel"] == OWNER and spawn["runner"] == "remote" and not spawn.get("cacheHit")
            and not spawn.get("status") and not spawn.get("exitCode"), "fresh remote action")
    platform = {prop["name"]: prop["value"] for prop in spawn["platform"]["properties"]}
    require(all(platform.get(key) == value for key, value in props.items()), "worker platform")
    executable, output = spawn["commandArgs"]
    require(spawn["listedOutputs"] == [output], "declared output")
    inp, = [file for file in spawn["inputs"] if file["path"] == executable]
    out, = spawn["actualOutputs"]
    require(out["path"] == output, "actual output")
    def contents(file):
        p = Path(file["path"])
        require(not p.is_absolute() and ".." not in p.parts and p.parts[0] == "bazel-out", "output path")
        data = (args.execroot / p).read_bytes()
        require(sha(data) == file["digest"]["hash"] and len(data) == int(file["digest"]["sizeBytes"]), "materialized bytes")
        return data
    binary = contents(inp)
    static_elf(binary, arch)
    witness = json.loads(contents(out))
    require(witness["Arch"] == arch and witness["Machine"] == MACHINES[arch][1] and witness["Directory"] == "/" + MARKER,
            "native observation")
    require(witness["ExecutableSHA256"] == sha(binary) and witness["ExecutableBytes"] == len(binary), "executed file")
    require(set(witness["Absent"]) == ABSENT and len(witness["Absent"]) == len(ABSENT), "tool absence")
    digest = spawn.get("digest", {}).get("hash")
    require(isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest), "remote action digest")
    return dict(arch=arch, invocation=args.invocation, owner=OWNER, worker=props, image=images[arch],
                action_digest=digest, observer_sha256=sha(binary), observer_bytes=len(binary), elf_machine=MACHINES[arch][0],
                output_sha256=out["digest"]["hash"], observed=witness)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    commands.add_parser("inventory").add_argument("layout", type=Path)
    cmd = commands.add_parser("action")
    cmd.add_argument("layout", type=Path)
    cmd.add_argument("repository")
    cmd.add_argument("arch", choices=MACHINES)
    cmd.add_argument("invocation")
    for field in ("execroot", "bep", "log"):
        cmd.add_argument(field, type=Path)
    parsed = parser.parse_args()
    print(json.dumps(inventory(parsed.layout) if parsed.mode == "inventory" else action(parsed), sort_keys=True))
