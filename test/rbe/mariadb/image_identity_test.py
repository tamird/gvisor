"""Check the original MariaDB image bytes and both configured Make contexts."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tarfile

from image_archive import archive, identity


def main() -> None:
    paths = [Path(value) for argument in sys.argv[1:] for value in argument.split()]
    expected_names = {
        f"qualification_mariadb_{kind}_{arch}.tar"
        for kind in ("archive", "context")
        for arch in ("amd64", "arm64")
    }
    if len(paths) != 4 or {path.name for path in paths} != expected_names:
        raise ValueError(f"Configured files differ: {paths}")
    by_name = {path.name: path for path in paths}
    upstream = Path(__file__).resolve().parent / "upstream"
    index_path = upstream / "index.json"
    if identity(index_path)["sha256"] != "a02fe89cb597d4375812b2eac90cf9d0775d4686daa7f7cc750ebbcad7525bbc":
        raise ValueError("Original MariaDB index differs")
    index = json.loads(index_path.read_text())
    proof = {"status": "INCOMPLETE", "architectures": []}
    destination = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"]) / "mariadb-image-identity.json"
    try:
        for arch in ("amd64", "arm64"):
            match [item for item in index["manifests"]
                   if item["platform"]["architecture"] == arch and item["platform"]["os"] == "linux"]:
                case [descriptor]:
                    pass
                case selected:
                    raise ValueError(f"Expected one {arch} manifest: {selected}")
            manifest_path = upstream / f"{arch}-manifest.json"
            manifest_id = identity(manifest_path)
            if ("sha256:" + manifest_id["sha256"], manifest_id["bytes"]) != (descriptor["digest"], descriptor["size"]):
                raise ValueError(f"{arch} manifest differs from its index")
            manifest = json.loads(manifest_path.read_text())
            config_path = upstream / f"{arch}-config.json"
            config_id = identity(config_path)
            if ("sha256:" + config_id["sha256"], config_id["bytes"]) != (manifest["config"]["digest"], manifest["config"]["size"]):
                raise ValueError(f"{arch} config differs from its manifest")
            original = json.loads(config_path.read_text())
            inherited = [
                {"sha256": layer["digest"].removeprefix("sha256:"), "bytes": layer["size"], "diff_id": diff_id}
                for layer, diff_id in zip(manifest["layers"], original["rootfs"]["diff_ids"], strict=True)
            ]
            row = {"architecture": arch, "upstream_manifest": manifest_id, "upstream_config": config_id}
            proof["architectures"].append(row)
            image = by_name[f"qualification_mariadb_archive_{arch}.tar"]
            config, files, elves = archive(image, row, inherited)
            if row["config_sha256"] != config_id["sha256"] or config != original:
                raise ValueError(f"{arch} full upstream config changed")
            if (config["architecture"], config["os"]) != (arch, "linux"):
                raise ValueError(f"{arch} image platform differs")
            if len(row["layers"]) != 8 or files or elves:
                raise ValueError(f"{arch} image has added or missing layers")
            with tarfile.open(image) as contents:
                image_manifest = json.load(contents.extractfile("manifest.json"))
            if image_manifest[0]["RepoTags"] != ["gvisor.dev/images/compatibility/mariadb/mariadb:latest"]:
                raise ValueError(f"{arch} image tag differs from the actual SQL consumer")
            context = by_name[f"qualification_mariadb_context_{arch}.tar"]
            row["context"] = identity(context)
            with tarfile.open(context) as contents:
                match [member for member in contents if member.isfile()]:
                    case [member] if member.name.removeprefix("./") == "images/compatibility/mariadb/mariadb/image.tar":
                        with contents.extractfile(member) as stream:
                            digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        if (digest, member.size) != (row["archive"]["sha256"], row["archive"]["bytes"]):
                            raise ValueError(f"{arch} Make context image differs from the archive")
                    case members:
                        raise ValueError(f"{arch} Make context contains unexpected regular files: {members}")
            row["status"] = "PASS"
        proof["status"] = "PASS"
    finally:
        destination.write_text(json.dumps(proof, indent=2) + "\n")
        print("MARIADB_IMAGE_IDENTITY " + json.dumps(proof), flush=True)


if __name__ == "__main__":
    main()
