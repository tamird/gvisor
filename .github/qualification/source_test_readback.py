"""Record the two remote outcomes and source frames from a blocked Go test."""
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
from xml.etree import ElementTree

from three_source_failures import project, witnesses

SOURCE = "66d356953fc8c9e155a8d15494c9f715173460cc"
OWNERS = {"//runsc/sandbox:sandbox_test", "//runsc/gofer/net:netgofer_test"}
work = Path(os.environ["RUNNER_TEMP"]) / "source-test-readback"
assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
_, names = witnesses()
report = {"source": SOURCE, "pr": "1be618324", "bazel_exit": int((work/"exit").read_text()), "tests": {}}
for line in (work / "run.bep").open():
    ev = json.loads(line)
    if "started" in ev:
        report["invocation"] = str(uuid.UUID(ev["started"]["uuid"]))
    if "testResult" not in ev:
        continue
    owner = ev["id"]["testResult"]["label"]
    assert owner in OWNERS and owner not in report["tests"]
    data = ev["testResult"]
    info = data.get("executionInfo", {})
    result = report["tests"][owner] = {"status": data["status"], "strategy": info.get("strategy"),
        "cached": bool(data.get("cachedLocally") or info.get("cachedRemotely"))}
    dest = Path("bazel-testlogs") / owner[2:].replace(":", "/")
    log = dest / "test.log"
    xml = dest / "test.xml"
    if xml.exists():
        cases = {}
        for n in ElementTree.parse(xml).iter("testcase"):
            name = n.get("name", "").split("/", 1)[0]
            if name in names:
                cases.setdefault(name, []).append(next((c.tag for c in n if c.tag in {"failure", "error", "skipped"}), "passed"))
        result["cases"] = cases
    if not log.is_file():
        continue
    raw = log.read_bytes()
    lines = raw.decode(errors="replace").splitlines()
    result["lines"] = len(lines)
    result["projection"] = project(raw)["rows"][:160]
    result["active_at_timeout"] = [s for s in sorted(names) if any(re.search(r"^\s*"+re.escape(s)+r"(?:/[^ ]+)?\s+\([0-9.]+[ms]+\)\s*$", l) for l in lines)]
    frames = []
    for i, l in enumerate(lines[:-1]):
        if not l.startswith(("gvisor.dev/gvisor/", "net.", "net/", "internal/poll.", "os/exec.")):
            continue
        # Store the function and source location, omitting runtime arguments.
        m = re.match(r"^([\w./]+(?:\.\(\*?\w+\))?(?:\.\w+)*)\(", l)
        loc = re.search(r"(\w+\.go):(\d+)",lines[i+1])
        if m and loc:
            frame = [m[1],loc[1],int(loc[2])]
            if frame not in frames:
                frames.append(frame)
    result["stack_frames"] = frames[:110]
(work/"receipt.json").write_text(json.dumps(report, indent=2)+"\n")
assert report["tests"].keys() == OWNERS
assert all(row["strategy"] == "remote" and not row["cached"] and row.get("lines", 0) > 0 for row in report["tests"].values())
