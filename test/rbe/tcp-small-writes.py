"""Compare the same pipelined write test on the specified git trees."""

import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET


def git(*args, data=None):
    return subprocess.run(["git", *args], input=data, capture_output=True, check=True).stdout


def prepare(name, root, sources):
    variant = sources[name]
    test = "test/syscalls/linux/tcp_socket.cc"
    paths = ["pkg/tcpip/transport/tcp/" + n + ".go" for n in ("endpoint", "rcv", "segment", "segment_queue")]
    git("restore", "--source=" + variant["base"], "--staged", "--worktree", "--", test, *paths)
    def apply(sha, changed):
        git("apply", "--index", "-", data=git("diff", sha + "^", sha, "--", *changed))
    apply(sources["coalesced"]["patches"][0], [test])
    for sha in variant["patches"]:
        apply(sha, paths)
    actual = git("write-tree").decode().strip()
    if actual != variant["tree"] or git("hash-object", test).decode().strip() != variant["fixture_blob"]:
        raise ValueError(f"{name} source mismatch: {actual}")
    (root / name / "tree").write_text(actual)


def contents(directory, name):
    path = directory / name
    return path.read_text(errors="replace").strip() if path.is_file() else ""


def verify(root, sources):
    names = {f"AllInetTests/SimpleTcpSocketTest.SmallSegmentsQueuedWhileNotReading/{n}" for n in (0, 1)}
    result = {}
    for name, variant in sources.items():
        directory = root / name
        xml = contents(directory, "test.xml")
        rows = []
        for case in ET.fromstring(xml).iter("testcase") if xml else ():
            failures = [f.text or f.get("message", "") for f in (*case.findall("failure"), *case.findall("error"))]
            skipped = case.find("skipped") is not None or case.get("status") == "notrun"
            status = "SKIP" if skipped else "FAIL" if failures else "PASS"
            queued = 21000 if status == "PASS" else None
            if len(failures) == 1 and "kTotal" in failures[0]:
                m = re.search(r"\bqueued\s+Which is:\s*(\d+)", failures[0])
                if m:
                    queued = int(m[1])
            rows.append(dict(case=case.get("classname", "") + "." + case.get("name", ""), status=status,
                             queued=queued, details=failures))
        observed = {r["case"] for r in rows}
        all_pass = bool(rows) and all(r["status"] == "PASS" for r in rows)
        exit_code = contents(directory, "exit")
        complete = (len(rows) == 2 and observed == names and contents(directory, "tree") == variant["tree"]
                    and exit_code == ("0" if all_pass else "3") and all(r["queued"] is not None for r in rows)
                    and (name != "native" or all_pass))
        log = contents(directory, "bazel.log")
        result[name] = dict(tree=variant["tree"], exit_code=exit_code, complete=complete, all_pass=all_pass,
                            expected=sorted(names), missing=sorted(names-observed), rows=rows,
                            end=contents(directory, "end"), invocations=list(dict.fromkeys(
                                re.findall(r"https://app.buildbuddy.io/invocation/([0-9a-f-]{36})", log))))
    verdict = dict(sources=sources, measurements=result, complete=all(r["complete"] for r in result.values()),
                   verified_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    (root / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    return verdict["complete"]


if __name__ == "__main__":
    root = Path(sys.argv[-1])
    sources = json.loads((root / "sources.json").read_text())
    if sys.argv[1] == "prepare":
        prepare(sys.argv[2], root, sources)
    else:
        sys.exit(not verify(root, sources))
