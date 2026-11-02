"""Recover the original goimports producer failure without another build."""

from datetime import datetime, timezone
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request
import zipfile

OUT = Path(os.environ["RUNNER_TEMP"]) / "goimports-original.json"
INV = "6047f2ad-51f7-4807-9347-cbb67e9efd00"
ZIP = "e6d2221ec6d894c51ad3ee46f6b3760726cd1996991c759207d84b561a1f4119"
row = {"status": "INCOMPLETE", "originalRun": 41396967661, "originalArtifact": 13119847172,
       "originalArchiveSha256": ZIP, "invocation": INV, "buildsExecuted": 0}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("redirect rejected")


def project(text: str) -> list[dict]:
    lines = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text).splitlines()
    selected = []
    for number, line in enumerate(lines, 1):
        if not re.search(r"ERROR:|Error in |\.bzl\", line \d+|\.bzl:\d+:\d+|\.go:\d+:\d+|BUILD(?:\.bazel)?:\d+:\d+|Analysis of target|Target .* failed|^(?:compilepkg|stdlib|go|compile|error|fatal error|ld|clang|gcc):", line):
            continue
        if re.search(r"api.?key|authorization|credential|password|token|remote_header|bes_header", line, re.I):
            continue
        line = re.sub(r"(?:https?|grpcs?)://[^\s'\"]+", "<url>", line)
        line = re.sub(r"/home/runner/[^ :\"]*/external/", "external/", line)
        line = re.sub(r"\b[\w.-]+\.[a-z]{2,}:\d+\b", "<endpoint>", line)
        selected.append({"line": number, "text": line[:900]})
        if len(selected) == 32:
            break
    return selected


def main() -> None:
    data = subprocess.check_output(["gh", "api", "repos/tamird/gvisor/actions/artifacts/13119847172/zip"])
    assert sha256(data).hexdigest() == ZIP
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ["goimports-bazel9.json"]
        original = json.loads(archive.read("goimports-bazel9.json"))
    assert original["source"] == "38d54a1b32203ba8081bad88b33cc66953d9bf8e"
    assert set(original["phases"]) == {"clean"}
    phase = original["phases"]["clean"]
    assert phase["invocation"] == INV and phase["exit"] == 1 and phase["actions"] == [] and phase["completed"] == []
    row["original"] = original
    opener = urllib.request.build_opener(NoRedirect)
    body = {"selector": {"invocation_id": INV}}
    chunks = []
    for _ in range(24):
        req = urllib.request.Request("https://app.buildbuddy.io/api/v1/GetLog", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        with opener.open(req, timeout=45) as response:
            value = json.loads(response.read(4 << 20))
        log = value["log"]
        ids = log.get("id", {})
        if ids:
            assert ids.get("invocationId", ids.get("invocation_id")) == INV
        chunks.append(log.get("contents", ""))
        token = value.get("nextPageToken", value.get("next_page_token", ""))
        if not token:
            break
        body["page_token"] = token
    else:
        raise ValueError("incomplete original log")
    log = "".join(chunks)
    row.update(providerLogSha256=sha256(log.encode()).hexdigest(), providerLogBytes=len(log.encode()), diagnostics=project(log))
    assert row["diagnostics"]
    row["status"] = "RECOVERED_LOGS"


try:
    main()
except Exception as exc:
    row["error"] = type(exc).__name__
finally:
    row["verifiedAt"] = datetime.now(timezone.utc).isoformat()
    OUT.write_text(json.dumps(row, indent=2) + "\n")
raise SystemExit(row["status"] != "RECOVERED_LOGS")
