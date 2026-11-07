"""Record the netgofer child stack if the peer-closed wait stalls."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
from xml.etree import ElementTree

from three_source_failures import project, witnesses

SOURCE = "457f831b47415cff28417dd36fed411f0b07bd59"
OWNERS = {"//runsc/gofer/net:netgofer_test"}
work = Path(os.environ["RUNNER_TEMP"]) / "source-test-readback"
assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
_, names = witnesses()
report = {"arm_base": "353c484b45fec1ba5ba19445ba03807a5f9638e0", "candidate": "9f3ed22283d9dfe28a001b9d3d126090c6c18951", "source_patch": "37d0d0fedafcaa8dcd4901d47d9bde87cbca85d2", "source": SOURCE, "bazel_exit": int((work/"exit").read_text()), "tests": {}}
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
    files = {entry["name"]: entry for entry in data.get("testActionOutput", [])}
    for path in (log, xml):
        if path.exists() and path.name in files:
            entry = files[path.name]
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            match = re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/(\d+)$", entry.get("uri", ""))
            ref = (match[1], int(match[2])) if match else (entry.get("digest"), int(entry.get("length", 0)))
            assert (digest, len(raw)) == ref
            result[path.name] = {"sha256": digest, "bytes": len(raw)}
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
    result["aarch64"] = "ARM_NETGOFER_HOST=aarch64" in lines
    result["controls"] = {key: sum(marker in line for line in lines) for key, marker in {
        "final_frame": "QUEUE empty=32 delivered=1 marker=5a ended=true",
        "allowed_fd_and_request": "CONFINED mode=allowed allowed=true",
        "wrong_fd_denied": "CONFINED mode=wrong-fd allowed=false",
        "wrong_request_denied": "CONFINED mode=wrong-command allowed=false",
    }.items()}
    allowed = set(re.findall(r"^func (Test\w+)\(", Path("runsc/gofer/net/netgofer_test.go").read_text(), re.M))
    for marker, key in ((r"=== RUN\s+", "started"), (r"--- PASS: ", "passed")):
        counts = {name: sum(bool(re.match(r"^\s*" + marker + name + r"(?:\s|$)", line)) for line in lines) for name in sorted(allowed)}
        result[key] = counts
    # Unindent the child log embedded in testing.T.Fatal. Do not retain
    # function arguments, full filesystem paths, register values or raw lines.
    start = next((i for i, line in enumerate(lines) if "child stalled in final Wait;" in line), None)
    result["child_stalled"] = start is not None
    result["child_goroutines"] = []
    if start is not None:
        result["quit_delivered"] = "SIGQUIT=<nil>" in lines[start]
        child = [line.strip() for line in lines[start+1:]]
        end = next((i for i, line in enumerate(child) if line.startswith(("--- FAIL:", "=== RUN"))), len(child))
        child = child[:end]
        result["child_signals"] = [marker for marker in ("SIGQUIT: quit", "SIGSYS", "TCP forwarder registered", "UDS socket peer closed", "fatal error:", "runtime stack:") if any(marker in line for line in child)]
        valid = ("gvisor.dev/gvisor/", "runtime.", "runtime/", "net.", "net/", "internal/poll.",
                 "sync.", "syscall.", "golang.org/x/sys/unix.", "os/exec.", "os.")
        group = None
        for i, line in enumerate(child[:-1]):
            if match := re.match(r"^goroutine (\d+).*?\[([^\]]{1,128})\]:", line):
                state = match[2].split(",",1)[0].strip()
                group = {"id": int(match[1]), "state": state if state in {"running","runnable","syscall","sync.WaitGroup.Wait","semacquire","chan receive","chan send","IO wait","select","sleep"} else "other", "frames":[]}
                result["child_goroutines"].append(group)
                continue
            if group is None or not line.startswith(valid) or "(" not in line:
                continue
            name = line.rsplit("(", 1)[0]
            loc = re.search(r"(?:/|^)([A-Za-z0-9_.]{1,90}\.(?:go|s)):(\d{1,7})\b", child[i+1])
            if loc and len(name) <= 220 and re.fullmatch(r"[\w./$()*\-\[\],~]+", name) and len(group["frames"]) < 30:
                group["frames"].append([name,loc[1],int(loc[2])])
        result["child_goroutines"] = [g for g in result["child_goroutines"] if any(
            f[0].startswith("gvisor.dev/gvisor/") for f in g["frames"])][:45]
    result["reset_lines"] = sum("Connection reset by peer" in line for line in lines)
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
assert result["aarch64"] and "test.log" in result and "test.xml" in result
assert result["status"] == "PASSED" and report["bazel_exit"] == 0 and not result["child_stalled"]
for name in ("TestRunProxyModeWithSeccomp", "TestFIONREADOnOnlyNetgoferFD", "TestNetgoferFinalFrameBehindEmptyRecords"):
    assert result["started"][name] == result["passed"][name] == 16
    assert result["cases"][name] and set(result["cases"][name]) == {"passed"}
assert set(result["controls"].values()) == {16}
