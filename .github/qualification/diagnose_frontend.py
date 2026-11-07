"""Project diagnostics from one original invocation into a fixed public vocabulary."""

import base64
import json
import os
from pathlib import Path
import re
import urllib.request


INV = "f072b843-1749-4b86-969a-8e2c3c71e816"
OWNERS = {
    "build": "//:build_test_presubmit_arm64",
    "yaml": "//:nogo_config_test_presubmit_arm64",
    "control": "//:buildkite_pipelines_test",
}
WORDS = set("""
action actions actual allocate already analysis are argument arguments artifact as at attempt available
bazel because been binary blob build builder cache cached can cannot can't canonically case chmod child class
collision command compiling concurrent conflict conflicting contains could create created creating cycle
dangling data declared dependency dependencies denied did different digest digests directories directory disk
does doesn't during empty encountered entry errno error errors exception execute executing execution executable
executables executor executors exists expected failed failure failures file files filesystem find first flags
for found from generated generating get graph had has have identical illegal in incompatible index input
inputs install internal invalid invocation io is it key known label last launch launching linked linker links
local location log look lookup lost manifest matching materialize materializing may metadata missing mnemonic
multiple must native new no node nodes non none not null of on only operation original otherwise out output
outputs overlap overlaps overlapping owner package parameter parent path paths permission platform platforms
process produced properties property read reading real regular relative remote report reported requesting
required requires resolved resolving resource resources returned root roots run runner runners running
runfile runfiles same sandbox scheduler second set setting shell should skipped source spawn spawning specified
start starting stat status stderr strategy successful supported symlink symlinks symbolic system target targets
test testing testlog testlogs testrunner tests that the their there this timed timeout to tool tools tree trees
tried true two unable unexpected unavailable undeclared under unique unknown unsupported unsuccessful use used
using valid value violated was were when while will with worker workers working workspace write written wrong
absolute ancestor ancestors ancestor's child children descendants descendant conflictingactions
forbidden forbiddenactioninputexception illegalargumentexception illegalstateexception ioexception
isnotdirectoryexception notdirectoryexception filenotfoundexception nosuchfileexception actioninputconflictexception
failedaction executionfailure lostinputs digestmismatch actionexecutionexception alreadyexistsexception
failedprecondition failed_precondition not_found inputoutputexception directorytree building slash separator
collides distinct duplicate nested top-level pathfragment cannotcreate processwrapper errno symlinked
prefixed prefix specified inconsistent runfilesmanifest supplier conflicts self native systrap testsetup
couldnotstart noclassdeffounderror bad died nodep symlinkaction spawnexecpoint executionphase teststatus
notexecutable builderror nonexistent disallowed runfilesdir runfilesdirectory fileset filesets emptyfile
long length exceeded too extension exit value dynamic filesystemexception collect collecting
""".split())
SIGNALS = ("error", "exception", "failed", "not found", "cannot", "could not", "conflict",
           "no such file", "not a directory", "permission denied", "duplicate", "already exists")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


client = urllib.request.build_opener(NoRedirect)


def request(method, body, cap):
    assert method in ("GetExecution", "GetEventLogChunk")
    r = urllib.request.Request("https://app.buildbuddy.io/rpc/BuildBuddyService/" + method,
                               data=json.dumps(body).encode(), headers={
                                   "Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["ACCESS"]})
    with client.open(r, timeout=12) as response:
        data = response.read(cap + 1)
    assert len(data) <= cap
    return json.loads(data)


def project(line):
    line = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", line).lower()
    if any(s in line for s in ("authorization", "api-key", "bes_header", "remote_header", "bearer ", "github_pat")):
        return ""
    tokens = []
    for word in re.findall(r"[a-z][a-z_']*", line[:1800]):
        token = word if word in WORDS else "?"
        if token != "?" or not tokens or tokens[-1] != "?":
            tokens.append(token)
    return " ".join(tokens[:94])


def main():
    report = {"invocation": INV, "owners": {}, "messages": []}
    for name, label in OWNERS.items():
        records = []
        for spelling in (label, "@@" + label):
            body = {"executionLookup": {"invocationId": INV, "targetLabel": spelling}}
            rows = request("GetExecution", body, 3_000_000).get("execution", [])
            for row in rows:
                if row.get("targetLabel") not in (label, "@@" + label) or row.get("actionMnemonic") != "TestRunner":
                    continue
                status = row.get("status", {})
                records.append({"rpc_code": int(status.get("code", 0)), "exit": int(row.get("exitCode", 0)),
                                "message": project(status.get("message", ""))})
            if records:
                break
        report["owners"][name] = records[:6]
    chunk = ""
    visited = set()
    for _ in range(8):
        if chunk in visited:
            break
        visited.add(chunk)
        data = request("GetEventLogChunk", {"invocationId": INV, "chunkId": chunk}, 3_000_000)
        buf = base64.b64decode(data.get("buffer", ""), validate=True).decode(errors="replace")
        for line in buf.splitlines():
            if any(signal in line.lower() for signal in SIGNALS):
                text = project(line)
                if text and text not in report["messages"] and len(report["messages"]) < 55:
                    report["messages"].append(text)
        chunk = data.get("previousChunkId", "")
        if not chunk or not re.fullmatch("[0-9a-fA-F]{1,24}", chunk):
            break
    report["chunks"] = len(visited)
    raw = json.dumps(report, sort_keys=True)
    assert len(raw) < 48_000 and os.environ["ACCESS"] not in raw
    Path(os.environ["RUNNER_TEMP"], "frontend-diagnostic.json").write_text(raw + "\n")


if __name__ == "__main__":
    main()
