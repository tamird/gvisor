"""Classify only the type of variation in one original ARM64 fixture log."""

import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request

INV = "466890c4-a777-4580-9c60-46796379a021"
SHA = "da5d78f56d0323b7f2e992f1e21970916e89debdf22cc60d82d6c4015559088f"
SIZE = 222937
TMP = Path(os.environ["RUNNER_TEMP"])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def retrieve():
    url = (f"https://remote.buildbuddy.io/file/download?invocation_id={INV}"
           f"&bytestream_url=bytestream%3A%2F%2Fremote.buildbuddy.io%2Fblobs%2F{SHA}%2F{SIZE}")
    request = urllib.request.Request(url, headers={"x-buildbuddy-api-key": os.environ.pop("BUILDBUDDY_API_KEY")})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=25) as response:
        data = response.read(SIZE + 1)
        assert response.status == 200 and len(data) == SIZE and hashlib.sha256(data).hexdigest() == SHA
    (TMP / "original-cmd.log").write_bytes(data)


def classify():
    assert "BUILDBUDDY_API_KEY" not in os.environ
    path = TMP / "original-cmd.log"
    try:
        data = path.read_bytes()
    finally:
        path.unlink(missing_ok=True)
    assert len(data) == SIZE and hashlib.sha256(data).hexdigest() == SHA
    bodies = data.decode().split("sandbox posture differs from expectation (-want +got):")[1:]
    # Diff prints the full Go literal after the abbreviated comparison.
    # Examine every divergence without persisting any thread or field value.
    quoted = r'"(?:\\.|[^"\\])*"'
    thread_array = re.compile(
        r'\bThreads:(?:[a-zA-Z_0-9]+\.)?Threads\{Uniform:false,\s*Divergences:\[\]string\{('
        + quoted + r'(?:,\s*' + quoted + r')*)\}\}'
    )
    entry = re.compile(r'thread "(?:\\.|[^"\\])*": Seccomp_filters = "[0-9]+", process has "[0-9]+"')
    if len(bodies) != 5:
        return False
    for body in bodies:
        collected = body.partition("Collected posture:")[2].lstrip().splitlines()[:1]
        matches = thread_array.findall(collected[0]) if collected else []
        if len(matches) != 1:
            return False
        try:
            if not all(entry.fullmatch(json.loads(s)) for s in re.findall(quoted, matches[0])):
                return False
        except ValueError:
            return False
    result = {"invocation": INV, "sha256": SHA, "size": SIZE, "comparisons": 5, "counterOnly": True}
    (TMP / "posture-category.json").write_text(json.dumps(result) + "\n")
    return True


if __name__ == "__main__":
    import sys
    if sys.argv[1] == "retrieve":
        retrieve()
    elif sys.argv[1] != "classify" or not classify():
        sys.exit(1)
