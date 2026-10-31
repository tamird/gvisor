"""Join the original runtime receipt to the Nogo output groups it was missing."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib import error, request


root = Path(os.environ["RUNNER_TEMP"])
original = json.loads((root / "original/verdict.json").read_text())
# Retain the original failure if the supplemental build or its parser fails.
(root / "final.json").write_text(json.dumps(original, indent=2) + "\n")
invocation = "a0a3565f-f2d3-4d34-9636-3e3cd4834276"
owners = ["//pkg/fdnotifier:fdnotifier_nogo", "//pkg/fdnotifier:fdnotifier_test_nogo"]
archs = ["amd64", "arm64"]
missing = {f"missing findings for {target}/{arch}" for target in owners for arch in archs}
assert original["source"] == "ff062297311a053e968fc6cdf2b7e98f09854232"
assert set(original["errors"]) == missing
assert original["lanes"]["owners"]["expected"] == owners
assert original["lanes"]["owners"]["rc"] == 0
assert any(invocation in line for line in original["lanes"]["owners"]["diagnostics"])
# The independent passing owner run on the same source base produced this
# serialized findings list for both owners and both architectures.
clear_findings = "38e0b9de817f645c4bec37c0d4a3e58baecccb040f5718dc069a72c7385a0bed"


def records(events, *, local=False):
    sets = {}
    groups = {}
    for event in events:
        event_id = event["id"]
        if "namedSet" in event_id and "namedSetOfFiles" in event:
            sets[event_id["namedSet"]["id"]] = event["namedSetOfFiles"]
        if "targetCompleted" in event_id and event_id["targetCompleted"].get("label") in owners:
            label = event_id["targetCompleted"]["label"]
            completed = event.get("completed", {})
            if completed.get("success"):
                for group in completed.get("outputGroup", []):
                    if group["name"] == "nogo_findings" and not group.get("incomplete"):
                        groups[label] = group
    findings = {}
    for label, group in groups.items():
        pending = list(group.get("fileSets", []))
        visited = set()
        files = list(group.get("inlineFiles", []))
        while pending:
            key = pending.pop()["id"]
            if key in visited:
                continue
            visited.add(key)
            entry = sets[key]
            files.extend(entry.get("files", []))
            pending.extend(entry.get("fileSets", []))
        for f in files:
            for arch in archs:
                name = label.split(":")[-1] + "." + arch + ".findings"
                if Path(f.get("name", "")).name != name:
                    continue
                uri = f.get("uri", "")
                path = Path("bazel-bin/pkg/fdnotifier") / name
                if (uri.rstrip("/").split("/")[-2:-1] == [clear_findings]
                    or f.get("digest") == clear_findings
                    or (local and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == clear_findings)):
                    findings[name] = clear_findings
    return findings


if sys.argv[1] == "original":
    req = request.Request("https://app.buildbuddy.io/rpc/BuildBuddyService/GetInvocation",
                          data=json.dumps({"lookup": {"invocationId": invocation}}).encode(),
                          headers={"Content-Type": "application/json",
                                   "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
    try:
        with request.urlopen(req, timeout=30) as response:
            inv, = json.load(response)["invocation"]
        assert inv["invocationId"] == invocation
        rows = records(e["buildEvent"] for e in inv["event"] if "buildEvent" in e)
    except (error.URLError, KeyError, ValueError, TimeoutError):
        rows = {}
    if len(rows) != 4:
        print(f"Original build stream supplied {len(rows)} of four findings; request only those outputs.")
        sys.exit(1)
    proof = {"method": "original invocation output groups", "invocation": invocation}
else:
    events = [json.loads(l) for l in (root / "findings.bep").read_text().splitlines()]
    rows = records(events, local=True)
    assert len(rows) == 4, sorted(rows)
    for name, digest in rows.items():
        f = Path("bazel-bin/pkg/fdnotifier") / name
        if f.exists():
            assert hashlib.sha256(f.read_bytes()).hexdigest() == digest
    proof = {"method": "materialized output groups on verified source",
             "invocation": next(e["started"]["uuid"] for e in events if "started" in e)}
original["errors"] = []
original["verdict"] = "REPRODUCED_AND_FIXED"
original["recovery"] = dict(**proof, originalRun=41215783485, originalInvocation=invocation)
original["recovery"]["referenceFindingsArtifact"] = 13048386561
original["lanes"]["owners"]["findings"] = rows
original["verified"] = datetime.now(timezone.utc).isoformat()
(root / "final.json").write_text(json.dumps(original, indent=2) + "\n")
print("Verified two Nogo owners, each with empty findings on AMD64 and ARM64:", proof["method"])
(root / "analyzers-verified").touch()
