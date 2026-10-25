"""Check the selected Debian runtime, native extensions, and Make contexts."""

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile

from image_archive import archive, identity, resolve


def status_text(image: Path) -> str:
    with tarfile.open(image) as outer:
        manifest = json.load(outer.extractfile("manifest.json"))[0]
        # The first added layer is the resolved Debian package payload.
        payload = outer.extractfile(manifest["Layers"][1]).read()
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as layer:
            matches = [member for member in layer if member.name.removeprefix("./").lstrip("/") == "var/lib/dpkg/status"]
            match matches:
                case [member]:
                    return layer.extractfile(member).read().decode()
                case _:
                    raise ValueError(f"Expected one generated dpkg status file: {matches}")


def main() -> None:
    paths = [Path(value) for argument in sys.argv[1:] for value in argument.split()]
    expected_names = {f"qualification_ruby_{kind}_{arch}.tar" for kind in ("archive", "context") for arch in ("amd64", "arm64")}
    if len(paths) != 4 or {path.name for path in paths} != expected_names:
        raise ValueError(f"Configured files differ: {paths}")
    by_name = {path.name: path for path in paths}
    inputs = Path(__file__).resolve().parent / "inputs"
    index_path = inputs / "index.json"
    if identity(index_path)["sha256"] != "62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad":
        raise ValueError("Pinned Debian index differs")
    index = json.loads(index_path.read_text())
    packages = json.loads((inputs / "native-packages.json").read_text())
    proof = {"status": "INCOMPLETE", "architectures": []}
    outputs = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    try:
        for arch, machine in (("amd64", 62), ("arm64", 183)):
            match [item for item in index["manifests"] if item["platform"]["architecture"] == arch and item["platform"]["os"] == "linux"]:
                case [descriptor]:
                    pass
                case selected:
                    raise ValueError(f"Expected one {arch} manifest: {selected}")
            manifest_path = inputs / f"{arch}-manifest.json"
            manifest_id = identity(manifest_path)
            if ("sha256:" + manifest_id["sha256"], manifest_id["bytes"]) != (descriptor["digest"], descriptor["size"]):
                raise ValueError(f"{arch} manifest differs from its index")
            manifest = json.loads(manifest_path.read_text())
            config_path = inputs / f"{arch}-config.json"
            config_id = identity(config_path)
            if ("sha256:" + config_id["sha256"], config_id["bytes"]) != (manifest["config"]["digest"], manifest["config"]["size"]):
                raise ValueError(f"{arch} base config differs from its manifest")
            original = json.loads(config_path.read_text())
            inherited = [{"sha256": layer["digest"].removeprefix("sha256:"), "bytes": layer["size"], "diff_id": diff_id}
                         for layer, diff_id in zip(manifest["layers"], original["rootfs"]["diff_ids"], strict=True)]
            if len(inherited) != 1:
                raise ValueError(f"Expected one pinned base layer, got {len(inherited)}")
            row = {"architecture": arch, "upstream_manifest": manifest_id, "upstream_config": config_id}
            proof["architectures"].append(row)
            image = by_name[f"qualification_ruby_archive_{arch}.tar"]
            config, files, elves = archive(image, row, inherited)
            if (config["architecture"], config["os"]) != (arch, "linux"):
                raise ValueError(f"{arch} image platform differs")
            if len(row["layers"]) != 3 or config["config"]["Cmd"] != ["/usr/bin/ruby3.3", "/server.rb"]:
                raise ValueError(f"{arch} image layers/command differ")
            if files["server.rb"]["sha256"] != "19defef9cbbf21a86de5f33c74cf5a8382f41ac28f7ca750a046930e949943c1":
                raise ValueError("HTTP server source changed")
            for name, data in elves.items():
                actual = int.from_bytes(data[18:20], "little")
                if data[4:6] != b"\x02\x01" or actual != machine:
                    raise ValueError(f"Unexpected {arch} ELF architecture at {name}: {actual}")
            selected_packages = [package for package in packages if f"Architecture: {arch}\n" in package["control"]]
            if len(selected_packages) != 3:
                raise ValueError(f"Expected Ruby/Puma/nio4r package identities for {arch}")
            for package in selected_packages:
                for expected in package["native_files"]:
                    name = expected["path"].removeprefix("./")
                    if (files[name]["sha256"], files[name]["bytes"]) != (expected["sha256"], expected["bytes"]):
                        raise ValueError(f"Native payload differs from pinned Debian package: {name}")
                for gemspec in package["gemspecs"]:
                    name = gemspec["path"].removeprefix("./")
                    if files[name]["sha256"] != hashlib.sha256(gemspec["content"].encode()).hexdigest():
                        raise ValueError(f"Gem specification differs: {name}")
                for notice in package["copyright_paths"]:
                    resolve(files, notice.removeprefix("./"))
            status = status_text(image)
            (outputs / f"{arch}-dpkg-status.txt").write_text(status)
            installed = {}
            for paragraph in status.strip().split("\n\n"):
                fields = dict(line.split(": ", 1) for line in paragraph.splitlines() if ": " in line and not line.startswith(" "))
                if fields["Architecture"] not in (arch, "all") or fields["Package"] in installed:
                    raise ValueError(f"Wrong architecture or duplicate package: {fields}")
                installed[fields["Package"]] = {key: fields[key] for key in ("Version", "Architecture")}
            expected_versions = {"base-files": "13.8+deb13u7", "ruby3.3": "3.3.8-2", "puma": "6.6.0-4", "ruby-sinatra": "4.1.1-5", "ruby-rackup": "2.1.0-4"}
            if set(installed) != set(expected_versions):
                raise ValueError(f"Generated dpkg roots differ: {installed}")
            for name, version in expected_versions.items():
                if installed[name]["Version"] != version:
                    raise ValueError(f"Unexpected installed version: {name}: {installed[name]}")
            row["dpkg_status_roots"] = installed
            row["dpkg_status_scope"] = "Generated status describes selected roots; the resolver lock retains the complete transitive closure."
            row["native_elf_count"] = len(elves)
            row["license_scope"] = "Package notices and common-license files retained; no catalog-policy acceptance inferred."
            row["copyright_paths"] = sorted(name for name in files if name.startswith("usr/share/doc/") and name.endswith("/copyright"))
            if not row["copyright_paths"] or not any(name.startswith("usr/share/common-licenses/") for name in files):
                raise ValueError("Package notices or common licenses missing")
            with tarfile.open(image) as contents:
                image_manifest = json.load(contents.extractfile("manifest.json"))
            if image_manifest[0]["RepoTags"] != ["gvisor.dev/images/image-test/ruby:latest"]:
                raise ValueError("Image tag differs from actual HTTP consumer")
            context = by_name[f"qualification_ruby_context_{arch}.tar"]
            row["context"] = identity(context)
            with tarfile.open(context) as contents:
                match [member for member in contents if member.isfile()]:
                    case [member] if member.name.removeprefix("./") == "images/image-test/ruby/image.tar":
                        with contents.extractfile(member) as stream:
                            digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        if (digest, member.size) != (row["archive"]["sha256"], row["archive"]["bytes"]):
                            raise ValueError("Make context image differs from runtime archive")
                    case members:
                        raise ValueError(f"Unexpected Make context files: {members}")
            row["status"] = "PASS"
        proof["status"] = "PASS"
    finally:
        (outputs / "ruby-distro-image-identity.json").write_text(json.dumps(proof, indent=2) + "\n")
        print("RUBY_DISTRO_IMAGE_IDENTITY " + json.dumps({"status": proof["status"], "architectures": [{key: value for key, value in row.items() if key != "files"} for row in proof["architectures"]]}), flush=True)


if __name__ == "__main__":
    main()
