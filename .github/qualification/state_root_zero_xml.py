"""Verify the named initialized-destination regression in two original XMLs."""
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.request
import xml.etree.ElementTree as ET

INV = "466890c4-a777-4580-9c60-46796379a021"
XML = {
    "amd64": ("22a25c76f48a041bbd1fcf7bd477c79733713f5a224f42bda274c9a4b7c41a19", 1087428),
    "arm64": ("4c4f4fb841f3cb6f9627880794e354b5846ea6e8f9a80cda576f6c541323d0ab", 1087428),
}
W = Path(os.environ["RUNNER_TEMP"])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def retrieve():
    token = os.environ.pop("BUILDBUDDY_API_KEY")
    opener = urllib.request.build_opener(NoRedirect)
    for arch, (sha, size) in XML.items():
        url = (f"https://remote.buildbuddy.io/file/download?invocation_id={INV}"
               f"&bytestream_url=bytestream%3A%2F%2Fremote.buildbuddy.io%2Fblobs%2F{sha}%2F{size}")
        req = urllib.request.Request(url, headers={"x-buildbuddy-api-key": token})
        with opener.open(req, timeout=25) as response:
            data = response.read(size + 1)
            assert response.status == 200 and len(data) == size and hashlib.sha256(data).hexdigest() == sha
        (W / (arch + ".xml")).write_bytes(data)


def check():
    assert "BUILDBUDDY_API_KEY" not in os.environ
    outcomes = {}
    for arch, (sha, size) in XML.items():
        p = W / (arch + ".xml")
        try:
            data = p.read_bytes()
        finally:
            p.unlink(missing_ok=True)
        assert len(data) == size and hashlib.sha256(data).hexdigest() == sha
        cases = list(ET.fromstring(data).iter("testcase"))
        target = [c for c in cases if c.get("name", "").split(".")[-1] == "TestRootZeroFields"]
        assert len(target) == 1
        case = target[0]
        skipped = case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "skipped"
        passed = not skipped and all(case.find(t) is None for t in ("failure", "error"))
        outcomes[arch] = {"passed": passed, "skipped": skipped, "suiteCases": len(cases), "xml": sha}
    result = {"invocation": INV, "source": "a39f38e89", "test": "TestRootZeroFields", "outcomes": outcomes}
    (W / "state-root-zero.json").write_text(json.dumps(result) + "\n")


if __name__ == "__main__":
    {"retrieve": retrieve, "check": check}[sys.argv[1]]()
