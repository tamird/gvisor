"""Read the immutable BuildBuddy result for the sidecar rebase bootstrap."""
import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import re

PARENT = "89f31a14-5091-4207-85b3-5fa852f0dca3"
HOST = "cb548161f537465fda2576793e6d6cea59a32260"
SOURCE = "59bc02b23955bc1d4c6a4c1423da514f38d05287"
SCRIPT_END = "exec python3 test/rbe/image_bootstrap.py"
DIAGNOSTIC = re.compile(
    r"fatal|error|fail|denied|unable|unavailable|not found|not supported|no such|"
    r"unrecognized|unknown|mismatch|invalid|could not|cannot|timed out|exit|"
    r"downloading|downloaded|fetching|checked out|HEAD is now|From https:|bazelisk|bazel [0-9]", re.I
)
# Preserve known error words, their order and public version numbers. Discard
# arbitrary arguments, URLs, paths, identifiers and environment data.
VOCAB = frozenset("""
a access action agent already allowed and anonymous architecture
argument artifact authentication authorization available bad base bazel bazelisk
blob bootstrap branch build building cache canceled cancelled cannot certificate check
checkout clone cmd code command commit completed connect connection container could
create credential denied did directory disconnected does docker download downloaded
downloading empty entrypoint error exec executable execute executed execution exists exit
exited expected failed failure fatal fetch fetching file find finished for found from get
git github head http image in incorrect input internal invalid is job launch layer linux
load manifest match matching mismatch missing name network no nonzero not now object
of on opening operation option or out permission platform precondition preparing process
pull pulling read reference refused registry remote repository request required resolving
restricted retried retries retry returned revision rev-parse runner running script
server service shell source started starting stat status stderr step stopping subcommand
success successfully support supported test than the this timed timeout tls to token tool
transport unable unavailable unexpected unknown unrecognized unsupported up use version
was when with workspace write x86_64 bash python3 setup symlink space left rate limit quota allocate rpc desc deadline exceeded
failedprecondition unauthenticated permissiondenied notfound unavailable unimplemented
""".split())


def call(key, method, selector, **options):
    assert method in ("GetInvocation", "GetLog")
    connection = http.client.HTTPSConnection("app.buildbuddy.io", timeout=30)
    try:
        connection.request("POST", "/api/v1/" + method,
                           json.dumps({"selector": selector, **options}),
                           {"Content-Type": "application/json", "x-buildbuddy-api-key": key})
        with connection.getresponse() as response:
            if response.status != 200:
                raise ValueError(f"HTTP {response.status}")
            data = response.read((4 << 20) + 1)
            assert len(data) <= 4 << 20
            return json.loads(data)
    finally:
        connection.close()


def project(text):
    clean = re.sub(r"\x1b\[[;\d]*[A-Za-z]", "", text)
    before, marker, after = clean.rpartition(SCRIPT_END)
    result = {"launcher_echo_complete": bool(marker), "diagnostics": []}
    # The service may echo the script before it runs. Record only fixed
    # bootstrap milestones before the echo; project actual output separately.
    for phase, region in (("setup", before), ("runtime", after)):
        selected = []
        for line in region.splitlines():
            if len(line) > 1500 or not DIAGNOSTIC.search(line):
                continue
            if any(word in line for word in ("BUILDBUDDY_", "--remote_header", "Authorization:", "QUALIFICATION", 'test "$(')):
                continue
            tokens = re.findall(r"[A-Za-z0-9_.-]+", line.lower())
            words = [t if t in VOCAB or t in ("8.8.1", "9.3.1") else "…" for t in tokens]
            projection = re.sub(r"(?:… ){2,}", "… ", " ".join(words)).strip(" …")
            if not projection:
                continue
            row = {"phase": phase, "words": projection[:250]}
            codes = re.findall(r"(?:exit(?:ed)?(?: with)? (?:status|code)|http(?: status)?)[: =]+(\d{1,3})\b", line, re.I)
            if codes:
                row["codes"] = [int(n) for n in codes[:3]]
            if row not in selected:
                selected.append(row)
        result["diagnostics"].extend(selected[:10] + selected[max(10, len(selected)-16):])
    return result


def main():
    key = os.environ.pop("BUILDBUDDY_API_KEY")
    output = {"source": SOURCE, "invocation": PARENT, "read": "INCOMPLETE"}
    try:
        selector = {"invocation_id": PARENT}
        [inv] = call(key, "GetInvocation", selector, include_artifacts=True)["invocation"]
        assert inv["id"]["invocationId"] == PARENT
        if re.fullmatch(r"[0-9a-f]{40}", inv.get("commitSha", "")):
            output["host_commit"] = inv["commitSha"]
        output["host_commit_verified"] = inv.get("commitSha") == HOST
        for field in ("invocationStatus", "bazelExitCode"):
            if isinstance(inv.get(field), str) and re.fullmatch(r"[A-Z_0-9]{1,90}", inv[field]):
                output[field] = inv[field]
        output["success"] = inv.get("success", False) is True
        for field in ("createdAtUsec", "updatedAtUsec", "durationUsec"):
            if field in inv:
                output[field] = int(inv[field])
        output["qualification_archives"] = sum(a.get("name") == "qualification.tar.gz" for a in inv.get("artifacts", []))
        page = "0000"
        pages = []
        seen = set()
        while page:
            assert page not in seen and len(pages) < 28
            seen.add(page)
            response = call(key, "GetLog", selector, page_token=page)
            pages.append(response.get("log", {}).get("contents", ""))
            page = response.get("nextPageToken", "")
        text = "".join(pages)
        output["console"] = {"pages": len(pages), "bytes": len(text.encode()), "sha256": hashlib.sha256(text.encode()).hexdigest(), **project(text)}
        output["read"] = "COMPLETE"
    except Exception as error:
        output["error"] = type(error).__name__
        if re.fullmatch(r"HTTP [0-9]{3}", str(error)):
            output["http"] = str(error)
        return 1
    finally:
        output["verified_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        Path("verdict.json").write_text(json.dumps(output, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
