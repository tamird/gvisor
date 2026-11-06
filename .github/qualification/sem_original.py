"""Record bounded diagnostics for the one owner invocation without running Bazel."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
from urllib import error, request

INVOCATIONS = {
    "old": "a36261ad-b5a8-4420-92f5-91393ee725ab",
    "destroy": "fedafcc1-6caa-490d-b841-4d50122edd03",
    "map": "6db1f103-4299-4e1e-976f-ad310d821d8c",
    "indexed": "28c94486-053c-4014-832a-402892a713d4",
    "analysis": "c4801fe2-ccbe-43b8-833b-b2116075377c",
}
HOST = "https://app.buildbuddy.io/rpc/BuildBuddyService/GetEventLogChunk"
key = os.environ["BUILDBUDDY_API_KEY"]
report = {"run": 42257227057, "product": "5411129f096b976b511b37d7bc58234a0e16cce1", "phases": {}}


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


def recover(inv):
    assert inv in INVOCATIONS.values()
    opener = request.build_opener(NoRedirect)
    cursor = ""
    seen = set()
    chunks = []
    buffers = []
    for _ in range(3):
        payload = {"invocationId": inv, "minLines": 3000}
        if cursor:
            assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cursor)
            payload["chunkId"] = cursor
        req = request.Request(HOST, data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json", "x-buildbuddy-api-key": key})
        with opener.open(req, timeout=15) as reply:
            raw = reply.read(4 * 1024 * 1024 + 1)
        assert len(raw) <= 4 * 1024 * 1024
        item = json.loads(raw)
        body = base64.b64decode(item["buffer"], validate=True)
        chunks.append({"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        buffers.append(body)
        cursor = item.get("previousChunkId", "")
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    text = b"".join(reversed(buffers)).decode(errors="replace")
    layout = re.search(r"SEM_LAYOUT member=(\d+) set=(\d+) cycle_allocs=([\d.]+)", text)
    state = re.findall(r"SEM_STATE (WIRE|GRAPH) active_rejected=(true|false) old=(true|false) count=(\d+) value=(\d+) pid=(\d+) stream=([A-Za-z0-9+/=]{1,32768})", text)
    rec = {"invocation": inv, "chunks": chunks, "layout": layout.groups() if layout else None,
           "tests": dict((name,status) for status,name in re.findall(r"--- (PASS|FAIL|SKIP): (Test(?:WaiterStorage|Unregister)) ", text)),
           "codecs": {id:{"reject":r,"old":old,"count":n,"value":v,"pid":pid,"stream_sha256":hashlib.sha256(base64.b64decode(enc,validate=True)).hexdigest()} for id,r,old,n,v,pid,enc in state},
           "owner_passed":bool(re.search(r"//pkg/sentry/kernel/semaphore:semaphore_test\s+PASSED",text)), "errors":[]}
    for line in text.splitlines():
        if "stream=" in line or "SEM_OLD_" in line or "SEM_STATE" in line or re.search(r"header|credential|auth(?:orization)?[:=]|bearer|token=|api[-_]key|argv|environment:|exec env", line, re.I):
            continue
        specific = re.search(r"(?:semaphore(?:_test)?\.go:\d+(?::\d+)?:|panic:|(?:mixed queue wake and cancellation|Resource Exhausted|DeadlineExceeded|FAILED_PRECONDITION|remote execution (?:timed out|failed)|RESOURCE_EXHAUSTED|socket connection closed|Out of memory|No space left on device))[^\n]*", line, re.I)
        match = specific or re.search(r"rpc error: code[^\n]*",line,re.I)
        msg = failure("ERROR: " + match[0]) if match else failure(line)
        if msg and msg not in rec["errors"] and len(rec["errors"])<16:
            rec["errors"].append(msg[:320])
    return rec


for phase, inv in INVOCATIONS.items():
    try:
        report["phases"][phase] = recover(inv)
    except (OSError, ValueError, KeyError, AssertionError) as err:
        report["phases"][phase] = {"invocation": inv, "error": type(err).__name__}
        if isinstance(err,error.HTTPError):
            report["phases"][phase]["http_status"] = err.code
raw = json.dumps(report, indent=2).encode()
assert key and key.encode() not in raw and len(raw)<35000 and b"stream=" not in raw and b"SEM_OLD_" not in raw
(Path(os.environ["RUNNER_TEMP"]) / "semaphore-original.json").write_bytes(raw)
print("Collected", len(report["phases"]), "original phase records.")
