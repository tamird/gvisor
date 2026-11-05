"""Exercise the real TCP Nogo owner with and without the endpoint lock at close timer cleanup."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback

ROOT = Path(os.environ["RUNNER_TEMP"])
PRODUCT = "b4672b800defab1365964be948bbcefe888e1325"
BASE = "90d4e2253f004cdc73a35dbc4050eab3e9a8495d"
CONTROL = "2e28b2d8df19dcedb294a7372539dbd39fca83ef"
FIELDS = ("finWait2Timer", "timeWaitTimer")
PKG = Path("pkg/tcpip/transport/tcp")
ENDPOINT = PKG / "endpoint.go"
OWNER = "//pkg/tcpip/transport/tcp:tcp_nogo"
ARCHES = ("amd64", "arm64")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def collect(arm, result):
    events = ROOT / (arm + ".bep")
    for line in events.read_text().splitlines():
        e = json.loads(line)
        if "started" in e:
            result["invocation"] = e["started"].get("uuid")
        if "action" in e and not e["action"].get("success", False):
            action = e["id"].get("actionCompleted", {})
            if len(result["failed_actions"]) < 8:
                result["failed_actions"].append({"owner": action.get("label"), "type": e["action"].get("type"),
                                                  "exit": e["action"].get("exitCode")})
        if "testResult" in e:
            attempt = e["testResult"]
            execution = attempt.get("executionInfo", {})
            result["attempts"].append(dict(owner=e["id"]["testResult"]["label"], status=attempt["status"],
                strategy=execution.get("strategy"), cached=attempt.get("cachedLocally", False) or execution.get("cachedRemotely", False)))
    logdir = Path("bazel-testlogs") / PKG / "tcp_nogo"
    for name in ("test.log", "test.xml"):
        out = logdir / name
        if out.is_file():
            raw = out.read_bytes()
            result[name] = {"bytes":len(raw), "sha256":hashlib.sha256(raw).hexdigest()}
    log = (logdir / "test.log").read_text()
    result["sections"] = re.findall(r"^(amd64|arm64)\.\.\.(PASS)?", log, re.M)
    valid = True
    for arch in ARCHES:
        output = Path("bazel-bin") / PKG / ("tcp_nogo." + arch + ".findings")
        if not output.is_file():
            valid = False
            result["findings"][arch] = {"missing":True}
            continue
        data = output.read_bytes()
        rows = json.loads(data)
        if rows is None:
            rows = []
        matches, located = [], []
        for finding in rows:
            pos = finding["Position"]
            field = result["access_lines"].get(str(pos["Line"]))
            basename = Path(pos["Filename"]).name
            message = finding["Message"]
            if len(located) < 18:
                located.append({"file":basename,"line":pos["Line"],"category":finding["Category"],
                                "field":next((name for name in FIELDS if "accessing " + name in message), None),
                                "missing_mutex":"must be locked" in message, "declared_mu":"invalid field access, mu (" in message})
            if (field and basename == "endpoint.go" and finding["Category"] == "checklocks"
                    and "invalid field access, mu (" in message
                    and "must be locked when accessing " + field + " (" in message
                    and (finding["GOOS"], finding["GOARCH"]) == ("linux", arch)):
                matches.append(field)
        result["findings"][arch] = {"count":len(rows), "matched":matches, "locations":located,
                                    "sha256":hashlib.sha256(data).hexdigest()}
        want = set(FIELDS) if arm == "unguarded" else set()
        valid &= set(matches) == want and len(rows) == len(matches)
    expected = "FAILED" if arm == "unguarded" else "PASSED"
    return (valid and sorted(result["sections"]) == [(a, "" if arm == "unguarded" else "PASS") for a in ARCHES]
            and len(result["attempts"]) == 1 and result["attempts"][0]["owner"] == OWNER
            and result["attempts"][0]["status"] == expected and not result["attempts"][0]["cached"]
            and result["attempts"][0]["strategy"] in {"processwrapper-sandbox", "linux-sandbox"}
            and result["exit"] == (3 if arm == "unguarded" else 0))


def run(arm, result):
    ref = CONTROL if arm == "unguarded" else PRODUCT
    subprocess.run(["git", "restore", "--source=" + ref, "--", ENDPOINT], check=True)
    if (git("rev-parse","HEAD") != PRODUCT or git("hash-object", str(ENDPOINT)) != git("rev-parse", ref + ":" + str(ENDPOINT))
            or git("diff","--name-only") != (str(ENDPOINT) if arm == "unguarded" else "")):
        raise ValueError("source identity")
    result["endpoint_blob"] = git("hash-object", str(ENDPOINT))
    lines = ENDPOINT.read_text().splitlines()
    start = lines.index("func (e *Endpoint) cleanupLocked() {")
    end = lines.index("}", start + 1)
    sites = {expression:field for field in FIELDS for expression in
             (f"if e.{field} != nil {{", f"e.{field}.Stop()")}
    result["access_lines"] = {str(i):sites[line.strip()] for i, line in enumerate(lines[start:end], start + 1)
                              if line.strip() in sites}
    if sorted(result["access_lines"].values()) != sorted(FIELDS * 2):
        raise ValueError("missing timer handle access in cleanupLocked")
    result["exit"] = subprocess.run(["timeout","--signal=INT","--kill-after=20","1350","bazel","test","--config=x86_64","-c","opt","--jobs=4",
        "--build_tag_filters=","--test_tag_filters=","--strategy=TestRunner=sandboxed","--remote_cache=","--remote_executor=","--bes_backend=","--nocache_test_results",
        "--flaky_test_attempts=1","--test_output=errors","--build_event_json_file=" + str(ROOT / (arm + ".bep")),OWNER]).returncode
    result["finished"] = datetime.now(timezone.utc).isoformat()
    result["verified"] = collect(arm, result)


def main():
    product_file = git("show", PRODUCT + ":" + str(ENDPOINT))
    guards = "\n\n".join(f"\tif e.{field} != nil {{\n\t\te.{field}.Stop()\n\t}}" for field in FIELDS)
    if product_file.count(guards) != 1:
        raise ValueError("expected exactly the two guarded Stop calls")
    control_file = product_file.replace(guards, "\te.mu.Unlock()\n" + guards + "\n\te.mu.Lock()")
    if (git("rev-parse", PRODUCT + "^") != BASE or git("rev-parse", CONTROL + "^") != PRODUCT
            or git("diff", "--name-only", PRODUCT, CONTROL) != str(ENDPOINT)
            or git("show", CONTROL + ":" + str(ENDPOINT)) != control_file):
        raise ValueError("unlocked close timer control source")
    if git("diff", "--name-only", BASE, PRODUCT) != str(ENDPOINT) or git("show", BASE + ":" + str(ENDPOINT)) != product_file.replace(
            "\n\t//\n\t// +checklocks:mu\n\tfinWait2Timer tcpip.Timer", "\n\tfinWait2Timer tcpip.Timer").replace(
            "\n\t//\n\t// +checklocks:mu\n\ttimeWaitTimer tcpip.Timer", "\n\ttimeWaitTimer tcpip.Timer"):
        raise ValueError("product declarations differ from base")
    report = {"product":PRODUCT, "base":BASE, "control":CONTROL, "owner":OWNER, "arms":{}}
    for arm in ("unguarded","guarded"):
        result = dict(attempts=[], failed_actions=[], sections=[], findings={}, verified=False)
        report["arms"][arm] = result
        try:
            run(arm, result)
        except Exception as exc:
            result["error"] = {"type":type(exc).__name__, "line":traceback.extract_tb(exc.__traceback__)[-1].lineno}
    report["verdict"] = "PASS" if all(r["verified"] for r in report["arms"].values()) else "INCOMPLETE"
    report["verified_at"] = datetime.now(timezone.utc).isoformat()
    (ROOT / "tcp-close-timers.json").write_text(json.dumps(report, indent=2) + "\n")
    print(report["verdict"], {a:r.get("exit") for a,r in report["arms"].items()})
    return report["verdict"] != "PASS"


if __name__ == "__main__":
    sys.exit(main())
