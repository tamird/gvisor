"""Classify only the type of variation in one original ARM64 fixture log."""

import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request

INV = "b31f8bfe-2484-48df-b737-5b9bc46052ac"
SHA = "63f622be0f08d7d6a3bf09d661164eae7c65708f774b8d0ad662d727d421dc33"
SIZE = 216982
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
    data = path.read_bytes()
    assert len(data) == SIZE and hashlib.sha256(data).hexdigest() == SHA
    path.unlink()
    text = data.decode(errors="replace")
    fields = {
        "Uid": "identity", "Gid": "identity",
        "CapInh": "capability", "CapPrm": "capability", "CapEff": "capability",
        "CapBnd": "capability", "CapAmb": "capability",
        "NoNewPrivs": "policy", "Seccomp": "policy", "Seccomp_filters": "counter",
    }
    fail_marker = "sandbox posture differs from expectation (-want +got):"
    categories = []
    for body in text.split(fail_marker)[1:]:
        diff = body.partition("Collected posture:")[0]
        # The collector formats a differing field as "<field> = <value>,
        # process has <value>". Ignore the values and all thread identifiers.
        keys = re.findall(r"\b(" + "|".join(fields) + r")\s*=", diff)
        if not keys:
            # go-cmp can abbreviate long strings. The original collected
            # Posture also contains the collector's same formatted strings.
            first = body.partition("Collected posture:")[2].lstrip().splitlines()[:1]
            keys = re.findall(r"\b(" + "|".join(fields) + r")\s*=", "".join(first))
        categories.append(sorted({fields[k] for k in keys}) or ["unclassified"])
    assert len(categories) == 5
    result = {"invocation": INV, "sha256": SHA, "size": SIZE, "comparisons": len(categories),
              "recognized": sum(c != ["unclassified"] for c in categories),
              "categories": sorted({category for group in categories for category in group})}
    (TMP / "posture-category.json").write_text(json.dumps(result) + "\n")


if __name__ == "__main__":
    import sys
    {"retrieve": retrieve, "classify": classify}[sys.argv[1]]()
