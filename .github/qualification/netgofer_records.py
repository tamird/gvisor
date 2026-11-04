"""Read the original finite Linux socket observations; no raw console is published."""

import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import qualification_report as report


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "results/verdict.json"
details = {"source": subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip(),
           "carrier": os.environ["GITHUB_SHA"], "owners": {}, "status": "INCOMPLETE"}
link = "//pkg/tcpip/link/fdbased:fdbased_test"
sidecar = "//runsc/gofer/net:netgofer_test"
names = ["Open", "Datagram", "Closed", "HalfClosed", "BeforeMarker", "Marked", "MarkedHalf", "FullEmptyBatch"]
prefix = "TestPrivateSeqpacketRecords"
expected = {link: {prefix + "/" + name for name in names}, sidecar: {"TestRunProxyModeEndToEnd"}}
events = tmp / "events.jsonl"
args = ["bazelisk", "test", "-c", "opt", "--jobs=4", "--remote_cache=", "--remote_executor=",
        "--strategy=TestRunner=local", "--build_tag_filters=", "--test_tag_filters=",
        "--nocache_test_results", "--test_timeout=60", "--flaky_test_attempts=1", "--runs_per_test=1",
        "--test_sharding_strategy=disabled", "--nobuild_event_json_file_path_conversion",
        f"--build_event_json_file={events}", "--test_filter=^(TestPrivateSeqpacketRecords|TestRunProxyModeEndToEnd)$",
        "--test_arg=-test.v", "--keep_going", link, sidecar]
env = {key: os.environ[key] for key in ("PATH", "HOME", "USER")}
env.update(LANG="C.UTF-8", TMPDIR=str(tmp))


def save():
    out.write_text(json.dumps(details, indent=2))


try:
    save()
    with (tmp / "native.log").open("w") as console:
        proc = subprocess.run(args, env=env, stdout=console, stderr=subprocess.STDOUT, timeout=2400)
    details.update(native_exit=proc.returncode, native_finished_at=utc())
    if proc.returncode:
        lines = (tmp / "native.log").read_text(errors="replace").splitlines()
        details["classes"] = [k for k, token in {"download": "Error downloading", "repository": "no such package", "compile": "GoCompilePkg", "undefined": "undefined:", "timeout": "TIMEOUT", "tests_failed": "tests FAILED"}.items() if any(token in ln for ln in lines)]
    save()
    assert events.is_file()
    runrows = {}
    for line in events.open():
        event = json.loads(line)
        if "testResult" not in event.get("id", {}):
            continue
        ident, payload = event["id"]["testResult"], event["testResult"]
        owner = ident["label"]
        assert owner in expected and owner not in runrows
        assert all(ident.get(k, 1) == 1 for k in ("run", "shard", "attempt"))
        runrows[owner] = payload
        details["owners"][owner] = {"config": ident["configuration"]["id"], "status": payload["status"],
            "strategy": payload.get("executionInfo", {}).get("strategy"), "start": payload.get("testAttemptStart"),
            "duration": payload.get("testAttemptDuration"), "fresh": not (payload.get("cachedLocally") or payload.get("executionInfo", {}).get("cachedRemotely"))}
    save()
    capture = report.capture(events, tmp / "canonical.jsonl", Path("bazel-out"), tmp / "reports")
    details["report_errors"] = len(capture["errors"])
    for owner, p in runrows.items():
        row = details["owners"][owner]
        for ext in ("xml", "log"):
            entry, = [r for r in capture["reports"] if r["identity"]["label"] == owner and r["name"] == "test." + ext]
            data = (tmp / entry["path"]).read_bytes()
            assert len(data) == entry["bytes"] and hashlib.sha256(data).hexdigest() == entry["sha256"]
            row[ext + "_sha256"] = hashlib.sha256(data).hexdigest()
            if ext == "xml":
                rows = {x.get("name"): x for x in ET.fromstring(data).iter("testcase")}
                row["passed"] = [n for n in sorted(expected[owner]) if n in rows and rows[n].get("status") not in ("notrun", "suppressed") and rows[n].get("result") != "suppressed" and not any(x.tag in ("failure", "error", "skipped") for x in rows[n])]
                row["extra_cases"] = len(set(rows) - expected[owner] - {prefix})
            if ext == "log" and owner == link:
                observations = {}
                current = None
                for line in data.decode(errors="replace").splitlines():
                    if m := re.search(r"=== RUN\s+TestPrivateSeqpacketRecords/([A-Za-z]+)", line):
                        current = m[1] if m[1] in names else None
                    if m := re.search(r"record batch=([01]) lengths=(\[[0-9 ]{0,32}\]) flags=(\[[0-9 ]{0,32}\]) delivered=([01]) continue=(true|false)", line):
                        assert current
                        observations.setdefault(current, []).append([int(m[1]), [int(i) for i in m[2][1:-1].split()], [int(i) for i in m[3][1:-1].split()], int(m[4]), m[5] == "true"])
                row["batches"] = observations
        save()
    assert set(runrows) == set(expected) and not capture["errors"] and proc.returncode == 0
    for owner, row in details["owners"].items():
        assert row["strategy"] in ("local", "standalone") and row["fresh"] and row["status"] == "PASSED"
        assert set(row["passed"]) == expected[owner] and not row["extra_cases"]
    groups = details["owners"][link]["batches"]
    assert {n: len(groups.get(n, [])) for n in names} == {n: 1 if n in ("Open", "Datagram") else 2 for n in names}
    details["status"] = "OBSERVATIONS_COMPLETE"
except Exception as exc:
    details["error_class"] = type(exc).__name__
finally:
    details["available_at"] = utc()
    save()
sys.exit(details["status"] != "OBSERVATIONS_COMPLETE")
