"""Stage the exact previous three-reader release and four-worker Nginx rootfs."""
import hashlib
import json
import os
from pathlib import Path
import tarfile
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("BuildBuddy byte request redirected")


def main():
    package = Path("test/rbe/nginx_quota")
    installer = Path(os.environ["RUNNER_TEMP"]) / "quota-install"
    installer.mkdir()
    outputs = json.loads((package / "release.json").read_text())
    fetch = urllib.request.build_opener(NoRedirect())
    for name, spec in outputs.items():
        params = urllib.parse.urlencode(dict(
            invocation_id="0c646642-fc20-443d-b7bc-77eb20b27f17",
            bytestream_url=spec["uri"],
            filename=Path(name).name,
        ))
        request = urllib.request.Request(
            "https://app.buildbuddy.io/file/download?" + params,
            headers={"x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]},
        )
        path = installer / name
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with fetch.open(request, timeout=90) as response, path.open("wb") as dst:
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                dst.write(chunk)
        assert digest.hexdigest() == spec["digest"] and path.stat().st_size == int(spec["length"]), name
        path.chmod(0o755)
    assert len(outputs) == 8
    with tarfile.open(package / "release.tar", "w") as tar:
        tar.add(installer / "release", "release")
    print("Verified all eight original release files")


if __name__ == "__main__":
    main()
