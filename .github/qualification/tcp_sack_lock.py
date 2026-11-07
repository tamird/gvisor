"""Exercise the real TCP Nogo owner with and without the SACK caller precondition."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(os.environ["RUNNER_TEMP"]) / "tcp-sack-lock"
PRODUCT = "6adb2b8822c12eef7653344809794a4d1e6f4000"
BASE = "3847ed738e1a59892b936aebb1b9164c4f31ee95"
PKG = Path("pkg/tcpip/transport/tcp")
CONNECT = PKG / "connect.go"
OWNER = "//pkg/tcpip/transport/tcp:tcp_nogo"
ARCHES = ("amd64", "arm64")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def collect(arm, result):
    events = ROOT / (arm + ".bep")
    for line in events.read_text().splitlines():
        e = json.loads(line)
        if "testResult" in e:
            attempt = e["testResult"]
            execution = attempt.get("executionInfo", {})
            result["attempts"].append(dict(owner=e["id"]["testResult"]["label"], status=attempt["status"],
                strategy=execution.get("strategy"), cached=attempt.get("cachedLocally", False) or execution.get("cachedRemotely", False)))
    log = (Path("bazel-testlogs") / PKG / "tcp_nogo/test.log").read_text()
    result["sections"] = re.findall(r"^(amd64|arm64)\.\.\.(PASS)?", log, re.M)
    valid = True
    for arch in ARCHES:
        data = (Path("bazel-bin") / PKG / ("tcp_nogo." + arch + ".findings")).read_bytes()
        rows = json.loads(data)
        if rows is None:
            rows = []
        matches = []
        for f in rows:
            pos = f["Position"]
            match = (f["Category"] == "checklocks" and Path(pos["Filename"]).name == "connect.go"
                     and pos["Line"] in result["access_lines"]
                     and "invalid field access" in f["Message"] and "mu" in f["Message"]
                     and "must be locked when accessing sack" in f["Message"]
                     and f["GOARCH"] == arch and f["GOOS"] == "linux")
            if match:
                matches.append(dict(line=pos["Line"], column=pos["Column"], message=f["Message"]))
        result["findings"][arch] = dict(count=len(rows), matched=matches, sha256=hashlib.sha256(data).hexdigest())
        want = set(result["access_lines"]) if arm == "guarded_field_without_contract" else set()
        valid &= len(rows) == len(matches) and {m["line"] for m in matches} == want
    expected = "FAILED" if arm == "guarded_field_without_contract" else "PASSED"
    return (valid and sorted(result["sections"]) == [(a, "" if arm == "guarded_field_without_contract" else "PASS") for a in ARCHES]
            and len(result["attempts"]) == 1 and result["attempts"][0]["owner"] == OWNER
            and result["attempts"][0]["status"] == expected and not result["attempts"][0]["cached"]
            and result["attempts"][0]["strategy"] in {"processwrapper-sandbox", "linux-sandbox"}
            and result["exit"] == (3 if arm == "guarded_field_without_contract" else 0))


def run(arm, result):
    ref = BASE if arm == "stock_without_contract" else PRODUCT
    subprocess.run(["git", "switch", "--detach", ref], check=True)
    if git("rev-parse", "HEAD") != ref or git("diff", "--name-only"):
        raise ValueError("checkout identity")
    marker = "// +checklocks:e.mu\nfunc (e *Endpoint) currentSACKBlocks("
    text = CONNECT.read_text()
    if text.count(marker) != 1:
        raise ValueError("caller precondition")
    if arm != "ordinary_product":
        text = text.replace(marker, "func (e *Endpoint) currentSACKBlocks(")
        CONNECT.write_text(text)
    result["source"] = ref
    result["connect_blob"] = git("hash-object", str(CONNECT))
    result["endpoint_blob"] = git("rev-parse", ref + ":" + str(PKG / "endpoint.go"))
    result["access_lines"] = [n for n, line in enumerate(text.splitlines(), 1)
                              if "return e.sack.Blocks[:e.sack.NumBlocks]" in line]
    if len(result["access_lines"]) != 1:
        raise ValueError("SACK history read")
    result["exit"] = subprocess.run([
        "bazel", "test", "--config=x86_64", "-c", "opt", "--jobs=4",
        "--lockfile_mode=off", "--build_tag_filters=", "--test_tag_filters=",
        "--strategy=TestRunner=processwrapper-sandbox", "--remote_cache=", "--remote_executor=",
        "--nocache_test_results", "--flaky_test_attempts=1", "--test_output=errors",
        "--build_event_json_file=" + str(ROOT / (arm + ".bep")), OWNER,
    ]).returncode
    result["finished"] = datetime.now(timezone.utc).isoformat()
    result["verified"] = collect(arm, result)


def main():
    report = {"product":PRODUCT, "base":BASE, "owner":OWNER, "arms":{}}
    for arm in ("ordinary_product", "stock_without_contract", "guarded_field_without_contract"):
        result = dict(attempts=[], sections=[], findings={}, verified=False)
        report["arms"][arm] = result
        try:
            run(arm, result)
        except (AssertionError, KeyError, OSError, ValueError, subprocess.SubprocessError) as exc:
            result["error"] = type(exc).__name__
        finally:
            subprocess.run(["git", "restore", "--source=HEAD", "--", str(CONNECT)], check=True)
        (ROOT / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    report["verdict"] = "PASS" if all(r["verified"] for r in report["arms"].values()) else "INCOMPLETE"
    report["verified_at"] = datetime.now(timezone.utc).isoformat()
    (ROOT / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(report["verdict"], {a:r.get("exit") for a,r in report["arms"].items()})
    return report["verdict"] != "PASS"


if __name__ == "__main__":
    sys.exit(main())
