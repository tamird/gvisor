"""Qualify terminal POLLERR with a queued asynchronous TCP error."""
import hashlib
import json
import os
import pathlib
import sys
import xml.etree.ElementTree as ET

TCP = pathlib.Path("pkg/tcpip/transport/tcp")
ARM = ("candidate", "no-peek")
MARK = "POLLERR after SO_ERROR with queued extended error"


def replace(path, before, after):
    source = path.read_text()
    assert source.count(before) == 1, (path, before)
    path.write_text(source.replace(before, after))


def prepare(arm):
    test = TCP / "test/e2e/tcp_test.go"
    before = "\tsockErr := ep.SocketOptions().DequeueErr()\n\tdefer sockErr.Payload.Release()"
    after = f'''\tif d := cmp.Diff(&tcpip.ErrHostUnreachable{{}}, ep.LastError()); d != "" {{
\t\tt.Fatalf("hard SO_ERROR (-want +got):\\n%s", d)
\t}}
\tif got := ep.Readiness(waiter.EventErr); got != waiter.EventErr {{
\t\tt.Errorf("{MARK} = %v, want %v", got, waiter.EventErr)
\t}}
{before}
\tif got := ep.Readiness(waiter.EventErr); got != 0 {{
\t\tt.Errorf("POLLERR after dequeuing extended error = %v, want 0", got)
\t}}'''
    replace(test, before, after)
    if arm == "no-peek":
        p = TCP / "endpoint.go"
        replace(p, "if hasError || e.SocketOptions().PeekErr() != nil {", "if hasError {")
    print(hashlib.sha256(test.read_bytes()).hexdigest())


def analyze(d, arm):
    q = d / arm
    if not (q / "exit").exists():
        return {"verified": False}
    result = {"exit": int((q / "exit").read_text()), "fixture": (q / "go-source").read_text().strip(),
              "start": (q / "start").read_text().strip(), "end": (q / "end").read_text().strip()}
    cases = []
    if (q / "test.xml").exists():
        for case in ET.parse(q / "test.xml").iter("testcase"):
            failed = [c for c in case if c.tag in ("error", "failure")]
            skipped = case.find("skipped") is not None
            cases.append({"name": case.get("name"), "skipped": skipped, "failed": bool(failed)})
    # rules_go test XML may omit failure output; the harness log contains the
    # assertion and its exit status. Record neither that log nor any command.
    lines = (q / "bazel.log").read_text(errors="replace").splitlines()
    exact = [x for x in lines if MARK in x and "want" in x]
    verdict = any(c["name"] == "TestConnectICMPError" and not c["skipped"] and c["failed"] == (arm == "no-peek") for c in cases)
    result.update(cases=cases, matched_assertion=bool(exact), verified=verdict and len(cases) == 1 and
                  result["exit"] == (3 if arm == "no-peek" else 0) and bool(exact) == (arm == "no-peek"))
    return result


if __name__ == "__main__":
    command, arg = sys.argv[1:]
    if command == "prepare":
        assert arg in ARM
        prepare(arg)
    else:
        d = pathlib.Path(arg)
        arms = {a: analyze(d, a) for a in ARM}
        ok = all(v["verified"] for v in arms.values()) and len({v.get("fixture") for v in arms.values()}) == 1
        (d / "verdict.json").write_text(json.dumps({"source": os.environ["CANDIDATE"], "arms": arms, "verified": ok}, indent=2) + "\n")
        if command == "final" and not ok:
            raise SystemExit("incomplete extended-error comparison")
