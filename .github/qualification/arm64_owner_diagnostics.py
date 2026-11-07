"""Read two original presubmit owners and report only typed test evidence."""

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

INVOCATION = "b31f8bfe-2484-48df-b737-5b9bc46052ac"
BLOBS = {
    "cmd.log": ("63f622be0f08d7d6a3bf09d661164eae7c65708f774b8d0ad662d727d421dc33", 216982),
    "cmd.xml": ("2d532073134a4fd936190ea2eb721f91c351bb09dfc81161cb60d4a995ba8903", 122106),
    "netgofer.log": ("d83c79f267b8132771553176cd3c9a65a7a337d3dace718fb5e6a4b131fa3672", 1274),
    "netgofer.xml": ("7918c8b44834006e54375dc5ea007fa2381ad0b9ab269c8cc167e944586a1402", 1764),
}
TMP = Path(os.environ["RUNNER_TEMP"])
STATUS = TMP / "arm-owners-http.json"
OUT = TMP / "arm-owners.json"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def retrieve():
    key = os.environ.pop("BUILDBUDDY_API_KEY")
    opener = urllib.request.build_opener(NoRedirect)
    statuses = {}
    for name, (sha, size) in BLOBS.items():
        url = (f"https://remote.buildbuddy.io/file/download?invocation_id={INVOCATION}"
               f"&bytestream_url=bytestream%3A%2F%2Fremote.buildbuddy.io%2Fblobs%2F{sha}%2F{size}")
        request = urllib.request.Request(url, headers={"x-buildbuddy-api-key": key})
        try:
            with opener.open(request, timeout=30) as response:
                data = response.read(size + 1)
                assert response.status == 200 and len(data) == size and hashlib.sha256(data).hexdigest() == sha
                (TMP / ("original-" + name)).write_bytes(data)
                statuses[name] = 200
        except urllib.error.HTTPError as exc:
            statuses[name] = exc.code
    STATUS.write_text(json.dumps(statuses))


def project(name, data):
    code = Path("pkg/test/sandboxposture/sandboxposture.go").read_text()
    fields = set(re.findall(r"(?m)^\t([A-Z]\w*)\s+(?:[A-Za-z_]|\[)", code))
    enums = set(re.findall(r'(?m)^\s*(?:FD|NS|ID|Root|Owner|CoreTag|Map|Groups|Limit)\w*\s*=\s*"([A-Za-z0-9_:/()>.-]+)"', code))
    enums.update(["seccomp-notify", "systrap-memory", "eventpoll", "eventfd", "NOFILE", "MEMLOCK",
                  "CAP_NET_ADMIN", "CAP_SYS_ADMIN", "CAP_SYS_PTRACE", "GODEBUG", "GOMAXPROCS", "GLIBC_TUNABLES", "TMPDIR",
                  "containermaxprocs=0", "asyncpreemptoff=1", "memprofilerate=0", "glibc.pthread.rseq=0",
                  "etc/localtime", "etc/", "proc/", "runsc-sandbox", "false", "true"])
    boot = Path("runsc/cmd/bootposture_test.go").read_text()
    net = Path("runsc/gofer/net/netgofer_test.go").read_text()
    cases = set(re.findall(r"(?m)^func (Test\w+)\(", net))
    suffixes = set(re.findall(r'(?:name:\s*|t.Run\()"([a-z0-9_]+)"', boot + net)) | {"sentry", "gofer"}
    cases.add("TestBootPosture")

    def known_case(value):
        parts = value.removeprefix("cmd.").removeprefix("net.").split("/")
        return "/".join(parts) if parts[0] in cases and all(p in suffixes for p in parts[1:]) else None

    text = data.decode(errors="replace")
    result = {}
    if name.endswith(".xml"):
        root = ET.fromstring(data)
        names = []
        for elem in root.iter("testcase"):
            case = known_case(elem.get("name", ""))
            if case:
                state = next((key for key in ("failure", "error", "skipped") if elem.find(key) is not None), "passed")
                names.append([case, state])
        result["cases"] = names[:120]
        text = "\n".join(root.itertext())

    signals = ["sandbox posture differs from expectation", "Error collecting posture", "Sandbox PID changed",
               "permission denied", "operation not permitted", "no such file or directory", "invalid argument",
               "not supported", "connection reset by peer", "test timed out", "TIMEOUT", "SIGQUIT",
               "goroutine", "fatal error", "panic:", "Collect", "KVM", "core scheduling"]
    markers, diffs, errors, frames = [], [], [], []
    active = None
    in_diff = False
    fields_nearby = []
    for line in text.splitlines():
        if len(line) > 5000 or "Collected posture:" in line or re.search(r"(?i)authorization:|x-buildbuddy-api-key|github_pat_", line):
            in_diff = False
            continue
        if m := re.search(r"(?:===\s+(RUN|NAME|CONT)|---\s+(PASS|FAIL|SKIP):)\s+([\w/]+)", line):
            case = known_case(m[3])
            active = case
            in_diff = False
            if case:
                markers.append([m[1] or m[2], case])
        if "sandbox posture differs from expectation" in line:
            in_diff = True
            fields_nearby = []
        if in_diff:
            keys = [t for t in re.findall(r"\b([A-Z]\w*)\s*:", line) if t in fields]
            if keys:
                fields_nearby = (fields_nearby + keys)[-6:]
            if m := re.match(r"^\s*([-+])\s*(.*)", line):
                body = m[2]
                values = sorted(s for s in enums if re.search(r"(?<!\w)" + re.escape(s) + r"(?!\w)", body))
                # Numeric limit pairs and small scalar field values only. Never project pointers,
                # free-form names, process IDs, arbitrary strings or the complete posture.
                numeric = re.findall(r'"([0-9]{1,10}/[0-9]{1,10})"', body)[:4]
                scalars = re.findall(r"(?<=: )([0-9]{1,5})(?=[,}])", body) if keys else []
                cpus = re.findall(r"\bGOMAXPROCS=([0-9]{1,4}|<--cpu-num>)", body)
                diffs.append([active, m[1], fields_nearby.copy(), values[:12], numeric, scalars[:4], cpus[:2]])
        tokens = [t for t in signals if t.casefold() in line.casefold()]
        src = re.findall(r"\b(bootposture_test\.go|sandboxposture\.go|expected\.go|netgofer_test\.go|netgofer\.go|proxy\.go):([0-9]{1,5})\b", line)
        if src:
            if name.startswith("netgofer"):
                frames.extend(src)
            if active or any(f == "bootposture_test.go" for f, _ in src):
                errors.append([active, src, tokens])
        elif tokens and name.startswith("netgofer"):
            errors.append([active, [], tokens])
    result.update(markers=markers[:180], diffs=diffs[:180], source_errors=errors[:130], frames=frames[:60])
    return result


def summarize():
    report = {"invocation": INVOCATION, "blobs": {}}
    statuses = json.loads(STATUS.read_text())
    for name, (sha, size) in BLOBS.items():
        entry = {"sha256": sha, "size": size, "http": statuses[name]}
        report["blobs"][name] = entry
        if entry["http"] == 200:
            path = TMP / ("original-" + name)
            data = path.read_bytes()
            assert len(data) == size and hashlib.sha256(data).hexdigest() == sha
            entry.update(project(name, data))
            path.unlink()
    OUT.write_text(json.dumps(report, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    {"retrieve": retrieve, "summarize": summarize}[sys.argv[1]]()
