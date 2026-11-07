"""Materialize only the nine ARM64 release blobs in the two original manifests."""

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import zipfile

WORK = Path(os.environ["RUNNER_TEMP"]) / "cross-native-guest"
INPUTS = Path("test/rbe/payload")
REMOTE = "500c9c79a833d1037e3b2532e8174a94b22398123f5e6b50fb8c408b7f43a046"
LOCAL = "9fdc1f767876e5f5ffd520eaae6afe2b790cca8617b1fff7703d09e36f22a813"
MANIFEST = "31bdb511152716c0bf87d2b6b127b2dab0fa92f707be03437cfc841dcd15db85"
INVOCATION = "d9b43462-e952-48ce-9e40-d22437114368"


def original(filename, checksum, member):
    data = (WORK / filename).read_bytes()
    assert len(data) < 64 << 10 and hashlib.sha256(data).hexdigest() == checksum
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.getinfo(member).file_size < 1 << 20
        return json.loads(archive.read(member))


def main():
    receipt = {"originalRemote": REMOTE, "originalLocal": LOCAL, "invocation": INVOCATION,
               "materialized": {}, "verified": False}
    try:
        remote = original("remote.zip", REMOTE, "release-arm64.json")
        local = original("local.zip", LOCAL, "lean-client.json")
        assert remote["source"] == local["base"] == "dd2be7670c1baaacebfdcab31bb6145ebce39037"
        assert local["source"] == "61ff148f0df0efa29c9f500914477525c5debe62" and local["both_releases_verified"]
        assert remote["target"] == "arm64" and remote["exec"] == "amd64" and remote["roots"] == ["//:release"]
        assert remote["verified"] and remote["all_native_copies"] and remote["all_original_links"]
        assert remote["invocation"] == INVOCATION
        same = local["arms"]["arm64"]["files"]
        assert len(same) == 9 and same.keys() == remote["files"].keys()
        files = {}
        for name, item in remote["files"].items():
            assert item["machine"] == same[name]["machine"] == 183 and item["load"] and item["entry"]
            assert not item["interp"] and not item["dynamic"] and same[name]["reference_matches"]
            assert item["bytes"] == same[name]["bytes"] and item["sha256"] == same[name]["sha256"]
            files[name] = {"sha256": item["sha256"], "bytes": item["bytes"]}
        manifest = (json.dumps(files, sort_keys=True, separators=(",", ":")) + "\n").encode()
        assert hashlib.sha256(manifest).hexdigest() == MANIFEST
        (INPUTS / "manifest.json").write_bytes(manifest)

        # The workflow verifies this original blob before entering the credentialed step.
        spec = importlib.util.spec_from_file_location("cas_inputs", WORK / "cas_inputs.py")
        cas = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cas)
        with tarfile.open(INPUTS / "release.tar.gz", "w:gz", compresslevel=1) as archive:
            for name, item in sorted(files.items()):
                receipt["reading"] = name
                body = cas.blob({"hash": item["sha256"], "sizeBytes": item["bytes"]}, INVOCATION)
                member = tarfile.TarInfo(name)
                member.mode = 0o755
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))
                receipt["materialized"][name] = item["sha256"]
        del receipt["reading"]
        receipt["verified"] = True
    except Exception as error:
        receipt["errorType"] = type(error).__name__
        if isinstance(getattr(error, "code", None), int):
            receipt["httpStatus"] = error.code
        raise SystemExit("Could not verify all nine original runtime inputs") from None
    finally:
        (WORK / "inputs.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
