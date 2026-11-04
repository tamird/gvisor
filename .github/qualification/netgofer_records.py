"""Bind the original loopback test result to its report and source assertion."""

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
sidecar = "//runsc/gofer/net:netgofer_test"
case = "TestRunProxyModeEndToEnd"
details = {
    "source": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "carrier": os.environ["GITHUB_SHA"], "original_artifact": 13293847371, "owner": sidecar,
    "expected": [case], "executed": [], "failed": [], "missing": [case], "harness_status": "INCOMPLETE",
}
markers = {
    "peer_close_cleanup": "runProxyMode() did not exit after UDS peer closed",
    "early_proxy_exit": "runProxyMode() exited early",
    "proxy_readiness": "timed out waiting for runProxyMode() to become ready",
    "proxy_exit_status": "runProxyMode() =",
    "echo_dial": "gonet.DialContextTCP(",
    "idle_dial": "gonet.DialTCP(idleAddr)",
    "echo_write": "clientConn.Write() failed",
    "echo_half_close": "clientConn.CloseWrite() failed",
    "echo_read": "io.ReadAll(clientConn) failed",
    "echo_response": "client received",
    "server_error": "server error:",
}
events = tmp / "events.jsonl"
args = ["bazelisk", "test", "-c", "opt", "--jobs=4", "--remote_cache=", "--remote_executor=",
        "--strategy=TestRunner=local", "--build_tag_filters=", "--test_tag_filters=",
        "--nocache_test_results", "--test_timeout=60", "--flaky_test_attempts=1", "--runs_per_test=1",
        "--test_sharding_strategy=disabled", "--nobuild_event_json_file_path_conversion",
        f"--build_event_json_file={events}", "--test_filter=^TestRunProxyModeEndToEnd$",
        "--test_arg=-test.v", "--keep_going", sidecar]
env = {key: os.environ[key] for key in ("PATH", "HOME", "USER")}
env.update(LANG="C.UTF-8", TMPDIR=str(tmp))


def save():
    out.write_text(json.dumps(details, indent=2))


try:
    save()
    with (tmp / "native.log").open("w") as console:
        proc = subprocess.run(args, env=env, stdout=console, stderr=subprocess.STDOUT, timeout=2400)
    details.update(native_exit=proc.returncode, native_finished_at=utc())
    save()
    assert events.is_file()
    runrows = {}
    for line in events.open():
        event = json.loads(line)
        if "testResult" not in event.get("id", {}):
            continue
        ident, payload = event["id"]["testResult"], event["testResult"]
        assert ident["label"] == sidecar and not runrows
        assert all(ident.get(k, 1) == 1 for k in ("run", "shard", "attempt"))
        runrows[sidecar] = payload
        details.update(configuration=ident["configuration"]["id"], product_owner_status=payload["status"],
                       strategy=payload.get("executionInfo", {}).get("strategy"), duration=payload.get("testAttemptDuration"),
                       fresh=not (payload.get("cachedLocally") or payload.get("executionInfo", {}).get("cachedRemotely")))
    save()
    assert runrows and details["fresh"] and details["strategy"] in ("local", "standalone")
    capture = report.capture(events, tmp / "canonical.jsonl", Path("bazel-out"), tmp / "reports")
    details["report_errors"] = len(capture["errors"])
    assert not capture["errors"]
    for ext in ("xml", "log"):
        entry, = [r for r in capture["reports"] if r["identity"]["label"] == sidecar and r["name"] == "test." + ext]
        data = (tmp / entry["path"]).read_bytes()
        assert len(data) == entry["bytes"] and hashlib.sha256(data).hexdigest() == entry["sha256"]
        details[ext + "_sha256"] = entry["sha256"]
        if ext == "xml":
            row, = list(ET.fromstring(data).iter("testcase"))
            assert row.get("name") == case and row.get("status") not in ("notrun", "suppressed")
            assert row.get("result") != "suppressed" and row.find("skipped") is None
            failed = row.find("error") is not None or row.find("failure") is not None
            details.update(executed=[case], missing=[], failed=[case] if failed else [])
        text = data.decode(errors="replace")
        details[ext + "_locations"] = sorted(set(int(x) for x in re.findall(r"netgofer_test\.go:(\d{1,4}):", text)))[:20]
        details[ext + "_classes"] = [name for name, message in markers.items() if message in text]
        save()
    if details["failed"]:
        assert details["native_exit"] == 3 and details["product_owner_status"] == "FAILED"
        assert details["log_locations"] and details["log_classes"]
    else:
        assert details["native_exit"] == 0 and details["product_owner_status"] == "PASSED"
    details["harness_status"] = "PASS"
except Exception as exc:
    details["harness_error"] = type(exc).__name__
    details["collector_line"] = exc.__traceback__.tb_lineno
finally:
    details["verified_at"] = utc()
    save()
sys.exit(details.get("native_exit") or int(details["harness_status"] != "PASS"))
