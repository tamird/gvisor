"""Bind the official Ruby bases, offline native gem layers and HTTP contexts."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tarfile

from test.rbe.ruby_distro.image_archive import archive, identity


def main() -> None:
    paths = [Path(value) for argument in sys.argv[1:] for value in argument.split()]
    expected_names = {
        name.format(arch=arch)
        for arch in ("amd64", "arm64")
        for name in (
            "qualification_ruby_archive_{arch}.tar",
            "qualification_ruby_context_{arch}.tar",
            "qualification_gems_{arch}.tar",
            "qualification_profile_{arch}.json",
        )
    }
    if len(paths) != 8 or {path.name for path in paths} != expected_names:
        raise ValueError(f"Configured files differ: {paths}")
    by_name = {path.name: path for path in paths}
    inputs = Path(__file__).resolve().parent / "inputs"
    index_path = inputs / "index.json"
    if identity(index_path)["sha256"] != "bceec7582aaa80630bb51a04e2df3af658e64c0640c174371776928ad3bd57b4":
        raise ValueError("Pinned official Ruby index differs")
    index = json.loads(index_path.read_text())
    abi = {row["architecture"]: row for row in json.loads((inputs / "ruby-abi.json").read_text())}
    gems = json.loads((inputs / "resolved.json").read_text())["gems"]
    proof = {"status": "INCOMPLETE", "architectures": []}
    output = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"]) / "ruby-official-image-identity.json"
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
                raise ValueError(f"{arch} manifest differs from index")
            manifest = json.loads(manifest_path.read_text())
            config_path = inputs / f"{arch}-config.json"
            config_id = identity(config_path)
            if ("sha256:" + config_id["sha256"], config_id["bytes"]) != (manifest["config"]["digest"], manifest["config"]["size"]):
                raise ValueError(f"{arch} config differs from manifest")
            original = json.loads(config_path.read_text())
            inherited = [
                {"sha256": layer["digest"].removeprefix("sha256:"), "bytes": layer["size"], "diff_id": diff_id}
                for layer, diff_id in zip(manifest["layers"], original["rootfs"]["diff_ids"], strict=True)
            ]
            row = {"architecture": arch, "upstream_manifest": manifest_id, "upstream_config": config_id}
            proof["architectures"].append(row)
            image = by_name[f"qualification_ruby_archive_{arch}.tar"]
            config, files, elves = archive(image, row, inherited)
            if (config["architecture"], config["os"]) != (arch, "linux"):
                raise ValueError(f"{arch} platform differs")
            if len(row["layers"]) != len(inherited) + 2 or config["config"]["Cmd"] != ["/usr/local/bin/ruby", "/server.rb"]:
                raise ValueError(f"{arch} image layers/command differ")
            for key, value in original["config"].items():
                # ArgsEscaped is deprecated Docker metadata for Windows commands.
                if key not in ("Cmd", "Env", "ArgsEscaped") and config["config"].get(key) != value:
                    raise ValueError(f"{arch} inherited config changed: {key}")
            expected_env = dict(value.split("=", 1) for value in original["config"]["Env"])
            expected_env.update(GEM_HOME="/opt/gems", GEM_PATH="/opt/gems")
            if dict(value.split("=", 1) for value in config["config"]["Env"]) != expected_env:
                raise ValueError(f"{arch} gem environment differs")
            if files["server.rb"]["sha256"] != "19defef9cbbf21a86de5f33c74cf5a8382f41ac28f7ca750a046930e949943c1":
                raise ValueError("HTTP server source changed")
            for name, data in elves.items():
                if data[4:6] != b"\x02\x01" or int.from_bytes(data[18:20], "little") != machine:
                    raise ValueError(f"Wrong {arch} native ELF at {name}")
            gem_tar = by_name[f"qualification_gems_{arch}.tar"]
            row["native_gem_tar"] = identity(gem_tar)
            layer = row["layers"][len(inherited)]
            if (layer["bytes"], layer["sha256"]) != (row["native_gem_tar"]["bytes"], row["native_gem_tar"]["sha256"]):
                raise ValueError(f"{arch} gem layer differs from the configured native build")
            profile_path = by_name[f"qualification_profile_{arch}.json"]
            row["native_profile"] = identity(profile_path)
            profile = json.loads(profile_path.read_text())
            if (profile["ruby"], profile["platform"]) != ("3.3.5", {"amd64": "x86_64-linux", "arm64": "aarch64-linux"}[arch]):
                raise ValueError(f"{arch} build Ruby differs")
            if {entry["file"]: entry["sha256"] for entry in profile["sources"]} != {gem["file"]: gem["sha256"] for gem in gems}:
                raise ValueError(f"{arch} source gem pins differ")
            if {entry["name"]: entry["version"] for entry in profile["gems"]} != {gem["name"]: gem["version"] for gem in gems}:
                raise ValueError(f"{arch} installed gem versions differ")
            expected_abi = {
                entry["path"]: (entry["bytes"], entry["sha256"])
                for entry in abi[arch]["files"] + abi[arch]["ruby_libraries"]
                if entry["path"].startswith("usr/local/include/ruby-3.3.0/") or entry["path"].endswith(("/rbconfig.rb", "/libruby.so.3.3.5"))
            }
            if {entry["path"]: (entry["bytes"], entry["sha256"]) for entry in profile["ruby_abi"]} != expected_abi:
                raise ValueError(f"{arch} build headers/library differ from pinned base")
            if len(profile["extensions"]) != 2:
                raise ValueError(f"{arch} must load two native extensions")
            for extension in profile["extensions"]:
                installed = files[extension["path"]]
                if (installed["bytes"], installed["sha256"]) != (extension["bytes"], extension["sha256"]) or "[libruby.so.3.3]" not in extension["readelf"]:
                    raise ValueError(f"{arch} native extension/ABI differs: {extension['path']}")
            row["extensions"] = profile["extensions"]
            row["gems"] = profile["gems"]
            with tarfile.open(image) as contents:
                if json.load(contents.extractfile("manifest.json"))[0]["RepoTags"] != ["gvisor.dev/images/image-test/ruby:latest"]:
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
                        raise ValueError(f"Unexpected Make context: {members}")
            row["status"] = "PASS"
        proof["status"] = "PASS"
    finally:
        output.write_text(json.dumps(proof, indent=2) + "\n")
        print("RUBY_OFFICIAL_IMAGE_IDENTITY " + json.dumps({"status": proof["status"], "architectures": [{key: value for key, value in row.items() if key != "files"} for row in proof["architectures"]]}), flush=True)


if __name__ == "__main__":
    main()
