"""Exercise the real TCP Nogo owner on the combined five-field positive source."""

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
PRODUCT = "f6e341d0b9ad32e924c88e863c00258982fc221e"
BASE = "90d4e2253f004cdc73a35dbc4050eab3e9a8495d"
FIELDS = ("userTimeout", "deferAccept", "tcpLingerTimeout", "finWait2Timer", "timeWaitTimer")
ENDPOINT_BLOB = "d003d9bd7215ac1ce65d1eca6e5e264084c8dba1"
PKG = Path("pkg/tcpip/transport/tcp")
ENDPOINT = PKG / "endpoint.go"
OWNER = "//pkg/tcpip/transport/tcp:tcp_nogo"
ARCHES = ("amd64", "arm64")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def collect(result):
    events = ROOT / "guarded.bep"
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
        result["findings"][arch] = {"count": len(rows), "sha256": hashlib.sha256(data).hexdigest(),
                                    "locations": [{"file": Path(f["Position"]["Filename"]).name,
                                                   "line": f["Position"]["Line"], "category": f["Category"]}
                                                  for f in rows[:18]]}
        valid &= not rows
    return (valid and sorted(result["sections"]) == [(a, "PASS") for a in ARCHES]
            and all(result.get(name, {}).get("bytes", 0) > 0 for name in ("test.log", "test.xml"))
            and len(result["attempts"]) == 1 and result["attempts"][0]["owner"] == OWNER
            and result["attempts"][0]["status"] == "PASSED" and not result["attempts"][0]["cached"]
            and result["attempts"][0]["strategy"] in {"processwrapper-sandbox", "linux-sandbox"}
            and result["exit"] == 0)


def run(result):
    result["endpoint_blob"] = git("hash-object", str(ENDPOINT))
    if (git("rev-parse", "HEAD") != PRODUCT or git("rev-parse", "HEAD^") != BASE
            or git("status", "--porcelain") or git("diff", "--name-only", BASE, PRODUCT) != str(ENDPOINT)
            or result["endpoint_blob"] != ENDPOINT_BLOB):
        raise ValueError("source identity")
    source = ENDPOINT.read_text()
    result["guarded_fields"] = [field for field in FIELDS if len(re.findall(
        r"^\t// \+checklocks:mu\n\t" + field + r"\s", source, re.M)) == 1]
    if tuple(result["guarded_fields"]) != FIELDS:
        raise ValueError("guarded declarations")
    result["exit"] = subprocess.run(["timeout","--signal=INT","--kill-after=20","1350","bazel","test","--config=x86_64","-c","opt","--jobs=4",
        "--build_tag_filters=","--test_tag_filters=","--strategy=TestRunner=sandboxed","--remote_cache=","--remote_executor=","--bes_backend=","--nocache_test_results",
        "--flaky_test_attempts=1","--test_output=errors","--build_event_json_file=" + str(ROOT / "guarded.bep"),OWNER]).returncode
    result["finished"] = datetime.now(timezone.utc).isoformat()
    result["verified"] = collect(result)


def main():
    result = dict(attempts=[], failed_actions=[], sections=[], findings={}, verified=False)
    report = {"product": PRODUCT, "base": BASE, "owner": OWNER, "guarded": result,
              "controls": {"three_options": "41990824589/13397499747", "two_timers": "42003794391/13403847961"}}
    try:
        run(result)
    except Exception as exc:
        result["error"] = {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
    report["verdict"] = "PASS" if result["verified"] else "INCOMPLETE"
    report["verified_at"] = datetime.now(timezone.utc).isoformat()
    (ROOT / "tcp-close-timers.json").write_text(json.dumps(report, indent=2) + "\n")
    print(report["verdict"], result.get("exit"))
    return report["verdict"] != "PASS"


if __name__ == "__main__":
    sys.exit(main())
