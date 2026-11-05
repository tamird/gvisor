"""Stage only the five public GNU runtime files from the pinned builder's base layer."""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import tarfile
import urllib.error
import urllib.request

import qualify as q

PINS = {
    "amd64": {
        "image": "x86_64",
        "manifest": "cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a",
        "layer": "cf1a5487baaf7d71eb78fd0888933c6cdecb40e59edb896511aa0ef4a896031e",
        "size": 29752476,
        "files": {
            "ld-linux-x86-64.so.2": "41daf7836f7fce21a40ca63a00c5caf21b05a35a76ccc075d86f1716212cdf01",
            "libc.so.6": "b2cf6c33b74d2f22543b7a469a75b538911e690f769d0b238843a49465b83793",
            "libpthread.so.0": "fbed9505adacada2abb37c6b43be2df6de81e742712c6025694ba48de62c7e78",
            "libdl.so.2": "3eaaa4aefd9c28f3867c96a427c3cbec9191cb61c4c9d0a2cb4a765d66064499",
            "libm.so.6": "3dd5511ae94785c9f921429b0f2b2f7aabb461b6f0e6de6dfdbef15f24bdfee6"
        }
    },
    "arm64": {
        "image": "aarch64",
        "manifest": "76cdcaa38feb618b971c97ca2570a7b2cfa4f85b8d9d8a4c86d9df261b2c7ca6",
        "layer": "2908c4fa36d87b4ba1206721a9efe744e0e9b35d1437148222276c744aa6efa0",
        "size": 27684723,
        "files": {
            "ld-linux-aarch64.so.1": "1f4fe51e11324b5bcc01ef1fd3859b5dd344ec16a8674d74ce618ef65016fbaf",
            "libc.so.6": "79fd4ecae5fb5fab4e17ec4f145d2b79026559dce52f01a478b0a04fef3dff06",
            "libpthread.so.0": "63c2e3d0d288006a9be6781c22e1e810a41b26a29963513dec7cdec10ee16cc8",
            "libdl.so.2": "8c36dc9f7c826ac79575bc079c3e35f3467a4700e59f6cde49996fa232317871",
            "libm.so.6": "4272b01b52991e751427228625655626ff4e0c51f8724fe14bb95b88b72a76ae"
        }
    }
}
DIRECTORY = Path("pkg/sentry/seccheck/sinks/remote/runtime")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def fetch(path, digest, limit, row, *, layer=False):
    row["http_status"] = None
    request = urllib.request.Request("https://gcr.io/v2/gvisor-presubmit/default_" + path + "/sha256:" + digest,
                                     headers={"Accept": "application/vnd.oci.image.manifest.v1+json"})
    # The public registry serves blobs through one relative Artifacts download.
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        stream = client.open(request, timeout=60)
    except urllib.error.HTTPError as err:
        with err:
            location = err.headers.get("Location", "")
            prefix = "/artifacts-downloads/namespaces/gvisor-presubmit/repositories/gcr.io/"
            if not (layer and err.code == 302 and location.startswith(prefix) and
                    all(re.fullmatch(r"[a-zA-Z0-9_-][a-zA-Z0-9_.:-]*", part)
                        for part in location[len(prefix):].split("/"))):
                raise
            row["relative_layer_redirect"] = err.code
        # Redirects are still disabled on this request. Never forward credentials.
        stream = client.open("https://gcr.io" + location, timeout=60)
    with stream:
        row["http_status"] = stream.status
        raw = stream.read(limit + 1)
    assert len(raw) <= limit and hashlib.sha256(raw).hexdigest() == digest
    return raw


def stage(arch):
    p = PINS[arch]
    row = {"status": "INCOMPLETE", "arch": arch, "manifest_sha256": p["manifest"], "layer_sha256": p["layer"],
           "files": {}, "phase": "manifest", "prior_preparation_artifact": 13417752542}
    try:
        manifest = json.loads(fetch(p["image"] + "/manifests", p["manifest"], 32768, row))
        row["phase"] = "layer_descriptor"
        layer = manifest["layers"][0]
        assert layer == {"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                         "digest": "sha256:" + p["layer"], "size": p["size"]}
        row["phase"] = "layer"
        raw = fetch(p["image"] + "/blobs", p["layer"], p["size"], row, layer=True)
        assert len(raw) == p["size"]
        row["phase"] = "members"
        dest = DIRECTORY / arch
        dest.mkdir(parents=True, exist_ok=False)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            for name, digest in p["files"].items():
                source = "usr/lib/" + p["image"] + "-linux-gnu/" + name
                entry = archive.getmember(source)
                assert entry.isfile() and 0 < entry.size < 5 * 1024 * 1024
                data = archive.extractfile(entry).read()
                machine = struct.unpack_from("<H", data, 18)[0]
                assert data[:6] == b"\x7fELF\x02\x01" and machine == {"amd64": 62, "arm64": 183}[arch]
                assert len(data) == entry.size and hashlib.sha256(data).hexdigest() == digest
                target = dest / name
                with target.open("xb") as output:
                    output.write(data)
                target.chmod(0o555)
                row["files"][name] = {"sha256": digest, "bytes": entry.size, "machine": machine}
        row["status"] = "PASS"
        row["phase"] = "complete"
    except urllib.error.HTTPError as exc:
        row["error"] = type(exc).__name__
        row["http_status"] = exc.code
    except (OSError, KeyError, ValueError, AssertionError, tarfile.TarError) as exc:
        row["error"] = type(exc).__name__
    q.save("runtime-input-" + arch, row)


if __name__ == "__main__":
    assert "BUILDBUDDY_API_KEY" not in os.environ and not (q.TMP / "private.bazelrc").exists()
    for arch in PINS:
        stage(arch)
