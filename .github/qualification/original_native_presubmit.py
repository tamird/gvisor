"""Retrieve and summarize two original presubmit outcomes without executing tests."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib import error, parse, request

WORK = Path(os.environ["RUNNER_TEMP"]) / "ordinary-profiles"
AMD = "e4a4dd1a-49f3-48dc-9f88-b73472a088fd"
ARM = "a85b5e95-1f11-4398-b60e-52856fda9ff7"
TARGET = "//runsc/gofer/net:netgofer_test"
SOURCE = "4eb27c4081e535d69c1257998a6c94256d9812e2"
API = "https://app.buildbuddy.io/rpc/BuildBuddyService/GetTarget"
SHA = re.compile(r"[0-9a-f]{64}")
LABEL = re.compile(r"(?:@@?[A-Za-z0-9._+~-]{1,100})?//[A-Za-z0-9_./+-]{0,150}:[A-Za-z0-9_./+-]{1,100}")
CASES = {"TestSpliceAndCloseWrite", "TestRunProxyModeEndToEnd", "TestRunProxyModeWithSeccomp",
         "TestProxyLimitsConnections", "TestProxyRejectsDisallowedDestinations",
         "TestNetgoferEarlyValidationClosesUDSFD"}
CASES.update("TestSpliceAndCloseWrite/" + sub for sub in (
    "half_close_and_bidirectional_copy", "aborts_on_transport_error", "close_write_fallback"))
CASES.update("TestProxyRejectsDisallowedDestinations/" + sub for sub in (
    "ipv4_current_network_subnet", "ipv4_link_local_unicast", "ipv4_local_interface_ip",
    "ipv6_link_local_unicast"))


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def fetch(req, limit, key):
    req.add_header("x-buildbuddy-api-key", key)
    try:
        with request.build_opener(NoRedirect).open(req, timeout=30) as response:
            raw = response.read(limit + 1)
    except error.HTTPError as exc:
        text = exc.read(400).lower()
        if b"permissiondenied" in text or b"unauthenticated" in text:
            raise RuntimeError("permission_denied") from None
        if b"notfound" in text:
            raise RuntimeError("not_found") from None
        raise RuntimeError(f"http_{exc.code}") from None
    if len(raw) > limit:
        raise RuntimeError("size_bound")
    return raw


def groups(query, key):
    req = request.Request(API, data=json.dumps(query).encode(), headers={"Content-Type":"application/json"})
    data = json.loads(fetch(req, 2*1024*1024, key))
    return data.get("targetGroups", data.get("target_groups", []))


def skipped(row, key):
    labels = []
    for g in groups({"invocationId": ARM, "status": 13}, key):
        if g.get("status") not in (13, "SKIPPED") or g.get("nextPageToken"):
            raise RuntimeError("unexpected_skip_group")
        for t in g.get("targets", []):
            label = t.get("metadata", {}).get("label", "")
            if not LABEL.fullmatch(label) or t.get("status") not in (13, "SKIPPED"):
                raise RuntimeError("invalid_skip")
            labels.append(label)
    if len(labels) > 2 or len(set(labels)) != len(labels):
        raise RuntimeError("skip_population_mismatch")
    row.update(labels=sorted(labels), reason="SKIPPED", indexed_both=(len(labels) == 2))


def retrieve_timeout(row, key):
    targets = [t for g in groups({"invocationId": AMD, "targetLabel": TARGET}, key)
               for t in g.get("targets", []) if t.get("metadata", {}).get("label") in (TARGET, "@@" + TARGET)]
    target, = targets
    event, = target.get("testResultEvents", target.get("test_result_events", []))
    ident = event["id"].get("testResult", event["id"].get("test_result", {}))
    result = event.get("testResult", event.get("test_result", {}))
    if ident.get("label") not in (TARGET, "@@" + TARGET) or result.get("status") not in (3, "TIMEOUT"):
        raise RuntimeError("wrong_test_result")
    if any(int(ident.get(k, 1)) != 1 for k in ("attempt", "shard", "run")):
        raise RuntimeError("wrong_attempt")
    row.update(status="TIMEOUT", run=1, shard=1, attempt=1)
    config = ident.get("configuration", {}).get("id", "")
    if SHA.fullmatch(config):
        row["configuration"] = config
    files = [f for f in result.get("testActionOutput", result.get("test_action_output", []))
             if f.get("name") == "test.log"]
    if not files:
        raise RuntimeError("no_test_log_output")
    blob, = files
    uri = re.fullmatch(r"bytestream://(?:remote|cache)\.buildbuddy\.io/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/(\d+)", blob.get("uri", ""))
    digest, size = blob.get("digest"), blob.get("length", 0)
    if isinstance(digest, dict):
        size, digest = digest.get("sizeBytes", 0), digest.get("hash", "")
    if uri:
        if digest and digest != uri[1]:
            raise RuntimeError("conflicting_digest")
        digest, size = uri.groups()
    if not SHA.fullmatch(digest or "") or not str(size).isdigit() or int(size)>16*1024*1024:
        raise RuntimeError("invalid_test_log_digest")
    size = int(size)
    row.update(test_log_sha256=digest, test_log_size=size)
    qs = parse.urlencode({"invocation_id": AMD, "bytestream_url":f"bytestream://remote.buildbuddy.io/blobs/{digest}/{size}"})
    data = fetch(request.Request("https://remote.buildbuddy.io/file/download?" + qs), size, key)
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError("log_digest_mismatch")
    assert key.encode() not in data
    (WORK / "netgofer.log").write_bytes(data)
    row["log_verified"] = True


def recover():
    key = os.environ.pop("BUILDBUDDY_API_KEY")
    assert key
    result = {"source":SOURCE, "amd64":{"invocation":AMD, "owner":TARGET}, "arm64":{"invocation":ARM}}
    for name, read in (("amd64", retrieve_timeout), ("arm64", skipped)):
        try:
            read(result[name], key)
        except RuntimeError as exc:
            message = str(exc)
            assert re.fullmatch(r"[a-z][a-z_0-9]{2,55}", message)
            result[name]["read_error"] = message
        except (KeyError, ValueError, TypeError, OSError, AssertionError) as exc:
            result[name]["read_error"] = type(exc).__name__
    data = json.dumps(result)
    assert key not in data and len(data)<6000
    (WORK / "read.json").write_text(data)


def stack_frames(lines):
    project = ("gvisor.dev/gvisor/runsc/gofer/net", "gvisor.dev/gvisor/pkg/tcpip",
               "gvisor.dev/gvisor/pkg/sync", "gvisor.dev/gvisor/pkg/rawfile",
               "gvisor.dev/gvisor/pkg/sleep", "golang.org/x/sys/unix")
    std = ("runtime", "testing", "syscall", "internal/poll", "internal/sync", "sync",
           "os", "os/exec", "net", "io", "time", "context")
    stack = []
    stacks = []
    for line, following in zip(lines, lines[1:]):
        if re.match(r"^goroutine \d+\b", line):
            if stack and stack not in stacks:
                stacks.append(stack)
            stack = []
        fun = re.fullmatch(r"(?:created by )?([A-Za-z0-9_./-]+\.(?:\(\*?[A-Za-z0-9_]+\)\.)?[A-Za-z_][A-Za-z0-9_.]{0,96})(?:\(.*\)| in goroutine [0-9]+)", line)
        loc = re.fullmatch(r"\s*(?:\S*/)?([A-Za-z0-9_]+\.(?:go|s)):(\d{1,7})(?:\s+\+0x[0-9a-f]+)?\s*", following)
        if not fun or not loc:
            continue
        name = fun[1]
        if not (any(name.startswith(p + ".") or name.startswith(p + "/") for p in project)
                or any(name.startswith(p + ".") for p in std)):
            continue
        stack.append([name, f"{loc[1]}:{loc[2]}"])
    if stack and stack not in stacks:
        stacks.append(stack)
    stacks.sort(key=lambda s: not any("runsc/gofer/net" in f for f, _ in s))
    return [s[:20] for s in stacks[:8]]


def project():
    assert "BUILDBUDDY_API_KEY" not in os.environ
    data = json.loads((WORK / "read.json").read_text())
    row = data["amd64"]
    if row.get("log_verified"):
        path = WORK / "netgofer.log"
        raw = path.read_bytes()
        path.unlink()
        assert hashlib.sha256(raw).hexdigest() == row["test_log_sha256"]
        lines = raw.decode(errors="replace").splitlines()
        started, finished, active = [], [], []
        for line in lines:
            m = re.match(r"^\s*(=== RUN\s+|--- (?:PASS|FAIL|SKIP):\s+)([A-Za-z0-9_/]+)(?:\s|$)", line)
            if m and m[2] in CASES:
                (started if m[1].startswith("===") else finished).append(m[2])
            m = re.fullmatch(r"\s*([A-Za-z0-9_/]+) \([0-9hms.]+\)", line)
            if m and m[1] in CASES and m[1] not in active:
                active.append(m[1])
        row.update(started=started[-20:], finished=finished[-20:], running_tests_in_timeout=active,
                   go_test_timeout=any("panic: test timed out after" in line for line in lines),
                   stacks=stack_frames(lines))
    data["read_complete"] = bool(row.get("log_verified") and data["arm64"].get("indexed_both"))
    out = json.dumps(data, indent=2)
    assert len(out.encode()) < 60000
    (WORK / "receipt.json").write_text(out + "\n")


if __name__ == "__main__":
    {"retrieve": recover, "project": project}[sys.argv[1]]()
