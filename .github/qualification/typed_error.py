"""Record bounded diagnostics for the one owner invocation without running Bazel."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
from urllib import error, request

INV = "7a525065-2fea-4f00-9203-e0018b077b6a"
HOST = "https://app.buildbuddy.io/rpc/BuildBuddyService/GetEventLogChunk"
key = os.environ["BUILDBUDDY_API_KEY"]
report = {"invocation": INV, "source": "bf20197d4c2ba3b36cbd9bb034f407a9c5cce5e6", "chunks": [], "findings": []}


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def failure(line):
    line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
    if re.search(r"header|credential|auth(?:orization)?[:=]|bearer|token=|api[-_]key|environment:|exec env", line, re.I):
        return None
    # Keep just the operating system diagnostic, named source location or Bazel target.
    markers = r"ERROR:|(?:GoCompilePkg|GoLink|GoStdlib|compilepkg|compile:|link:).*?(?:error|failed)|(?:pkg/|external/)[A-Za-z0-9_+./-]+\.go:\d+:|fatal error:|(?:rpc error: code|Caused by:)|(?:^|\s)(?:undefined:|undefined reference|no such target|No space left on device|No matching toolchains)"
    hit = re.search(markers, line, re.I)
    if hit is None:
        return None
    s = line[hit.start():].strip()
    # Never export a tool command, URLs, absolute runner paths, or opaque tokens.
    s = re.split(r"\b(?:bazel-out/\S*/builder(?:_reset)?|/bin/(?:sh|bash)|exec env|arguments skipped)\b", s, maxsplit=1)[0]
    s = re.sub(r"(?:https?|grpcs?)://\S+", "[service]", s)
    s = re.sub(r"/(?:home|root|tmp|var|buildbuddy|workspace)/[^\s:]*?/(?:execroot/[^/\s]+/|external/)", "external/", s)
    s = re.sub(r"[A-Za-z0-9_+=-]{35,}", "[id]", s)
    return s[:410].strip()


try:
    opener = request.build_opener(NoRedirect)
    cursor = ""
    seen = set()
    buffers = []
    for _ in range(5):
        payload = {"invocationId": INV, "minLines": 4000}
        if cursor:
            assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cursor)
            payload["chunkId"] = cursor
        req = request.Request(HOST, data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json", "x-buildbuddy-api-key": key})
        with opener.open(req, timeout=18) as reply:
            raw = reply.read(5 * 1024 * 1024 + 1)
        assert len(raw) <= 5 * 1024 * 1024
        item = json.loads(raw)
        body = base64.b64decode(item["buffer"], validate=True)
        report["chunks"].append({"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        buffers.append(body)
        cursor = item.get("previousChunkId", "")
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    for chunk in reversed(buffers):
        for line in chunk.decode(errors="replace").splitlines():
            finding = failure(line)
            if finding and finding not in report["findings"] and len(report["findings"]) < 28:
                report["findings"].append(finding)
except (OSError, ValueError, KeyError, AssertionError) as err:
    report["reader_error"] = type(err).__name__
    if isinstance(err, error.HTTPError):
        report["http_status"] = err.code
raw = json.dumps(report, indent=2).encode()
assert key and key.encode() not in raw and len(raw) < 17000
(Path(os.environ["RUNNER_TEMP"]) / "typed-shutdown-original.json").write_bytes(raw)
print("Read", len(report["chunks"]), "original log chunks;", len(report["findings"]), "diagnostics.")
