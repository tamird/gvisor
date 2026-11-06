"""Recover the one public release payload recorded by run 42186258349."""
import hashlib
import os
from pathlib import Path
import urllib.error
import urllib.request

SHA = "790e542f54eac80ab8d816a5740b5fbf6e9d81f6b58e102600d9e52c4ad99e42"
SIZE = 84396638
INV = "aa23a90f-d672-4eb5-a218-2338c8e3172b"
ROOT = Path(os.environ["RUNNER_TEMP"]) / "release-witness"

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None

def main():
    # This is the same endpoint and namespace to which Bazel wrote the archive.
    # Its exact digest and length were measured after installing all nine files.
    url = f"https://remote.buildbuddy.io/file/download?invocation_id={INV}&bytestream_url=bytestream%3A%2F%2Fremote.buildbuddy.io%2Fblobs%2F{SHA}%2F{SIZE}"
    request = urllib.request.Request(url, headers={"x-buildbuddy-api-key":os.environ.pop("BUILDBUDDY_API_KEY")})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=120) as response:
            assert response.status == 200
            with (ROOT/"original.tar.bz2").open("xb") as output:
                remaining = SIZE
                digest = hashlib.sha256()
                while remaining:
                    part = response.read(min(remaining, 1024*1024))
                    if not part:
                        raise ValueError("short original archive")
                    digest.update(part)
                    output.write(part)
                    remaining -= len(part)
                assert not response.read(1) and digest.hexdigest() == SHA
    except urllib.error.HTTPError as err:
        raise SystemExit(f"original release archive receipt: HTTP {err.code}") from None

if __name__ == "__main__":
    main()
