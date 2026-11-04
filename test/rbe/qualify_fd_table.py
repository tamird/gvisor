"""Run selected package owners and verify required cases from their original XML."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

import qualification_report as report

pr, architecture, source, rc, destination = sys.argv[1:]
assert pr in ("ordinary", "race", "nogo", "negative"), pr
negative = pr == "negative"
analyzer = pr in ("nogo", "negative")
package = "//pkg/sentry/state/stateipc:"
cases = ["TestRead", "TestReadv", "TestWrite", "TestWritev", "TestClientWatchdog"]
case_owners = {package + ("stateipc_nogo" if analyzer else "stateipc_test"): [] if analyzer else cases}
owners = list(case_owners)
required = {(label, case) for label, cases in case_owners.items() for case in cases}
d = Path(destination)
d.mkdir()
def now():
    return datetime.now(timezone.utc).isoformat()
def git(*args):
    return subprocess.check_output(["git", *args])
def save(name, value):
    (d/name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")

r = {"pr": pr, "architecture": architecture, "source": source, "expected": owners, "required_cases": sorted(required), "status": "INCOMPLETE"}
try:
    r["worker_image"] = os.environ["WORKER_IMAGE"]
    assert git("rev-parse", "HEAD").decode().strip() == source
    assert source == ("0b078813b37387fb3d30e079f43eccb812e4d035" if negative else "26a9823e3e565b7e709316489bd5756696d81797"), source
    r["tree"] = git("rev-parse", "HEAD^{tree}").decode().strip()
    (d/"before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    assert not (d/"before").read_bytes()
    (d/"commit").write_bytes(git("cat-file", "commit", source))
    bazel = ["bazel", "--bazelrc=" + rc]
    args = ["test", *(["--config=race"] if pr == "race" else []), "--nocache_test_results", "--test_sharding_strategy=disabled", "--runs_per_test=1", "--flaky_test_attempts=1",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--build_event_json_file=" + str(d/"events.raw"),
            "--build_event_json_file_path_conversion=false",
            "--remote_download_regex=.*[/](test[.](log|xml)|test[.]outputs/.*)$", *owners]
    with (d/"command.log").open("wb") as l:
        code = subprocess.run(["timeout", "-k", "20", "1500", *bazel, *args], stdout=l, stderr=subprocess.STDOUT).returncode
    r.update(native_exit=code, execution_finished_at=now())
    (d/"exit").write_text(str(code)+"\n")
    (d/"after").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (d/"head").write_bytes(git("rev-parse", "HEAD"))
    (d/"args").write_text("\n".join(args)+"\n")
    (d/"expected").write_text("\n".join(owners)+"\n")
    save("context", {"expected_source": source, "command_kind": "test", "execution_finished_at": r["execution_finished_at"],
                     "commit_file": "commit", "source_before": "before", "source_after": "after", "final_head": "head",
                     "native_exit": "exit", "capture_exit": "capture", "executed_arguments": "args"})
    log=(d/"command.log").read_text(errors="replace")
    r["invocations"] = re.findall(r"Invocation ID: ([0-9a-f-]{36})", log)
    r["console_sha256"] = hashlib.sha256((d/"command.log").read_bytes()).hexdigest()
    r["hints"] = [h for h in ("RESOURCE_EXHAUSTED", "no such target", "Permission denied", "not found", "signal: killed") if h.lower() in log.lower()]
    clean=d/"events.jsonl"
    outroot=Path(subprocess.check_output(bazel+["info","output_base"],stderr=subprocess.DEVNULL,text=True).strip())
    m=report.capture(d/"events.raw",clean,outroot,d/"reports")
    (d/"capture").write_text(str(int(bool(m["errors"])))+"\n")
    v=report.verdict(clean,report.manifest_path(clean),d/"context",d/"expected")
    r["report"]={k:v[k] for k in ("status","executed","case_counts") if k in v}
    r["report_problem_counts"] = {k:len(v.get(k, [])) for k in ("failures", "missing", "skips", "errors")}
    r["reports"] = [{k:f[k] for k in ("identity", "name", "sha256", "bytes")} for f in m["reports"]]
    r["executions"]=[]
    for line in clean.read_text().splitlines():
        event=json.loads(line)
        if "testResult" not in event: continue
        row=event["testResult"]
        meta=row.get("executionInfo",{})
        r["executions"].append({"owner":event["id"]["testResult"]["label"], "identity":event["id"]["testResult"], "status":row["status"],
                                "timing":{k:row[k] for k in ("testAttemptStart", "testAttemptStartMillisEpoch", "testAttemptDuration", "testAttemptDurationMillis") if k in row},
                                "remote":meta.get("strategy")=="remote",
                                "cached":bool(row.get("cachedLocally") or meta.get("cachedRemotely"))})
    records=[]
    for f in m["reports"]:
        if f["name"]!="test.xml": continue
        for case in ET.parse(d/f["path"]).iter("testcase"):
            records.append({"name":case.get("name"),"owner":f["identity"]["label"],
                            "result":"skipped" if case.get("status")=="notrun" or case.get("result")=="suppressed" else next((c.tag for c in case if c.tag in ("skipped","failure","error")),"pass")})
    r["cases"]=records
    logs = "\n".join((d/f["path"]).read_text(errors="replace") for f in m["reports"] if f["name"] == "test.log")
    r["race_config"] = "--config=race" in args
    r["bazelrc_blob"] = git("rev-parse", "HEAD:.bazelrc").decode().strip()
    assert "test:race --@io_bazel_rules_go//go/config:race --@io_bazel_rules_go//go/config:pure=false" in Path(".bazelrc").read_text().splitlines()
    if pr == "race":
        ordinary = json.loads((d.parent / "final-ordinary/result.json").read_bytes())
        baseline = ordinary.get("executions", [])
        if baseline:
            config = lambda attempt: attempt["identity"]["configuration"]["id"]
            r["different_from_ordinary"] = config(baseline[0]) != config(r["executions"][0])
            assert r["different_from_ordinary"]
    r["architecture_sections"] = {arch: bool(re.search(r"(?m)^" + arch + r"\.\.\.PASS\s*$", logs)) for arch in ("amd64", "arm64")}
    found={(row["owner"],row["name"]) for row in records if row["result"]=="pass"}
    r["missing_required"]=sorted(required-found)
    ran={e["owner"] for e in r["executions"] if e["remote"] and not e["cached"]}
    assert len(r["executions"]) == 1, r["executions"]
    assert ran == set(owners), ran
    if negative:
        lines = Path("pkg/sentry/state/stateipc/client.go").read_text().splitlines()
        accesses = [i for i, line in enumerate(lines, 1) if line.strip() == "delete(rw.client.files, rw)"]
        assert len(accesses) == 1
        r["unlocked_delete_line"] = accesses[0]
        findings = [line for line in logs.splitlines() if "GOOARCH=" in line]
        r["missing_lock"] = {
            arch: sum(f"client.go:{accesses[0]}:" in line and "checklocks:" in line
                      and "invalid field access" in line and "filesMu" in line and "when accessing files" in line
                      and f"GOOARCH={arch}, GOOS=linux" in line for line in findings)
            for arch in ("amd64", "arm64")
        }
        r["finding_count"] = len(findings)
        r["status"] = "EXPECTED_FAILURE" if (code == 3 and v["status"] == "FAIL" and not v.get("missing")
                and r["executions"][0]["status"] == "FAILED" and len(findings) == 2
                and any(row["result"] in ("failure", "error") for row in records)
                and set(r["missing_lock"].values()) == {1}) else "FAIL"
    else:
        if analyzer:
            matched = all(r["architecture_sections"].values())
        else:
            r["passed_in_log"] = {name: bool(re.search(r"(?m)^--- PASS: " + name + r" \(", logs)) for name in cases}
            matched = (all(r["passed_in_log"].values()) and found == required and len(records) == len(cases))
        r["status"] = "PASS" if (code == 0 and v["status"] == "PASS" and matched and records
                and all(c["result"] == "pass" for c in records) and r["executions"][0]["status"] == "PASSED") else "FAIL"
except (AssertionError, KeyError, OSError, ValueError, ET.ParseError, subprocess.CalledProcessError) as exc:
    r["collector_error"]=type(exc).__name__
    if r.get("native_exit",0): r["status"]="FAIL"
finally:
    r["verified_at"]=now()
    save("result.json",r)
sys.exit(int(r["status"] not in ("PASS", "EXPECTED_FAILURE")))
