"""Verify the rebased image fixtures and all Make entry points."""
import hashlib
import io
import json
import struct
import subprocess
import sys
import tarfile
from pathlib import Path

MARIA = "030d4e7349a20e3bb3008763f5cd2d96a546c5f7"
HELPERS = "8dc316391447193c51882d606da918974087f372"
LABELS = {
    "compatibility/mariadb/mariadb": "//images/compatibility:mariadb_context",
    "benchmarks/redis": "//images/basic:benchmark_redis_context",
    "benchmarks/hey": "//images/benchmarks/hey:hey_context",
    "benchmarks/profile-helper": "//images/benchmarks/profile-helper:profile_helper_context",
    "gpu/ollama/client": "//images/gpu/ollama/client:client_context",
}
HELPER_EXECUTABLES = {
    "benchmarks/hey": ("bin/hey", "/bin/hey"),
    "benchmarks/profile-helper": ("usr/bin/profilehelper", "/bin/bash"),
    "gpu/ollama/client": ("usr/bin/httpclient", "/usr/bin/httpclient"),
}
MANIFESTS = {
    "amd64": "63b29a06fbde176102a20d90fee92b2bac3a112a4f47816cac18da9b70767393",
    "arm64": "f3761402303ebafc3bb4cb174afae399d71acd72b260f19139b5337dc39065f1",
}


def verify_mariadb(arch, image, record, config_bytes):
    upstream = Path(__file__).with_name(arch + "-manifest.json").read_bytes()
    assert hashlib.sha256(upstream).hexdigest() == MANIFESTS[arch]
    manifest = json.loads(upstream)
    assert "sha256:" + hashlib.sha256(config_bytes).hexdigest() == manifest["config"]["digest"]
    assert len(record["Layers"]) == len(manifest["layers"]) == 8
    for path, expected in zip(record["Layers"], manifest["layers"], strict=True):
        member = image.getmember(path)
        assert member.size == expected["size"]
        digest = hashlib.file_digest(image.extractfile(member), "sha256").hexdigest()
        assert "sha256:" + digest == expected["digest"]


def verify_helper(name, arch, image, record, config):
    path, command = HELPER_EXECUTABLES[name]
    assert config["config"]["Cmd"] == [command], config["config"]
    layer_data = image.extractfile(record["Layers"][-1]).read()
    with tarfile.open(fileobj=io.BytesIO(layer_data)) as layer:
        (member,) = [item for item in layer if not item.isdir()]
        assert member.name.removeprefix("./") == path and member.isfile() and member.mode == 0o755
        header = layer.extractfile(member).read(20)
        assert header[:6] == b"\x7fELF\x02\x01"
        assert struct.unpack_from("<H", header, 18)[0] == {"amd64": 62, "arm64": 183}[arch]


def verify_context(name, arch):
    package, target = LABELS[name][2:].split(":")
    with tarfile.open(Path("bazel-bin", package, target + ".tar")) as context:
        (member,) = [m for m in context if m.isfile()]
        assert member.name.removeprefix("./") == f"images/{name}/image.tar"
        with tarfile.open(fileobj=context.extractfile(member)) as image:
            (record,) = json.load(image.extractfile("manifest.json"))
            assert record["RepoTags"] == [f"gvisor.dev/images/{name}:latest"]
            config_bytes = image.extractfile(record["Config"]).read()
            config = json.loads(config_bytes)
            assert (config["os"], config["architecture"]) == ("linux", arch)
            if name == "compatibility/mariadb/mariadb":
                verify_mariadb(arch, image, record, config_bytes)
            elif name in HELPER_EXECUTABLES:
                verify_helper(name, arch, image, record, config)
    print(f"{name} {arch} archive and Make context verified", flush=True)


def main():
    match sys.argv[1:]:
        case ["mariadb"]:
            source = MARIA
            names = ["compatibility/mariadb/mariadb", "benchmarks/redis"]
        case ["helpers"]:
            source = HELPERS
            names = list(HELPER_EXECUTABLES)
        case _:
            raise ValueError(sys.argv[1:])
    subprocess.run(["git", "switch", "--detach", source], check=True)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
    for cpu, arch in (("x86_64", "amd64"), ("aarch64", "arm64")):
        database = subprocess.check_output(
            ["make", "-np", "-f", "tools/images.mk", "ARCH=" + cpu, "--eval=selection: ;", "selection"],
            text=True,
        ).splitlines()
        for name in dict.fromkeys([*names, "benchmarks/redis"]):
            for operation in ("tag", "pull", "rebuild", "load", "test", "local-image", "push"):
                mapping = f"{operation}-{name.replace('/', '_')}: _image_context_target := {LABELS[name]}"
                assert mapping in database, mapping
        targets = [LABELS[name] for name in names]
        subprocess.run(["bazel", "build", "--jobs=4", "--config=" + cpu, *targets], check=True)
        for name in names:
            verify_context(name, arch)


if __name__ == "__main__":
    main()
