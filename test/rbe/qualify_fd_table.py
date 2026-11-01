"""Run selected package owners and verify required cases from their original XML."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

import qualification_report as report

pr, architecture, source, rc, destination = sys.argv[1:]
assert pr in ("fd-table-positive", "fd-table-negative")
negative = pr == "fd-table-negative"
case_owners = {"//pkg/sentry/kernel:kernel_test_nogo": []} if negative else {
    "//pkg/sentry/kernel:kernel_test_nogo": [],
    "//pkg/sentry/syscalls/linux:linux_test_nogo": [],
    "//runsc/boot:boot_test_nogo": [],
}
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
    assert source == ("98f81699970b454161f8f43dfb35de2301d74016" if negative else "bd501903137f05f736ed90612194bcb9bc5d9916")
    r["tree"] = git("rev-parse", "HEAD^{tree}").decode().strip()
    (d/"before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    assert not (d/"before").read_bytes()
    (d/"commit").write_bytes(git("cat-file", "commit", source))
    bazel = ["bazel", "--bazelrc=" + rc]
    args = ["test", "--nocache_test_results", "--test_sharding_strategy=disabled",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--build_event_json_file=" + str(d/"events.raw"),
            "--build_event_json_file_path_conversion=false",
            "--remote_download_regex=.*[/](test[.](log|xml)|test[.]outputs/.*)$", *owners]
    with (d/"command.log").open("wb") as l:
        code = subprocess.run(bazel + args, stdout=l, stderr=subprocess.STDOUT).returncode
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
    r["failure_excerpt"] = [line[:1000] for line in log.splitlines() if line.startswith("ERROR:") or ": error:" in line or "not found" in line.lower() or "No such file" in line or "undefined reference" in line][:24] if code else []
    r["hints"] = [h for h in ("RESOURCE_EXHAUSTED", "no such target", "Permission denied", "not found", "signal: killed") if h.lower() in log.lower()]
    clean=d/"events.jsonl"
    outroot=Path(subprocess.check_output(bazel+["info","output_base"],stderr=subprocess.DEVNULL,text=True).strip())
    m=report.capture(d/"events.raw",clean,outroot,d/"reports")
    (d/"capture").write_text(str(int(bool(m["errors"])))+"\n")
    v=report.verdict(clean,report.manifest_path(clean),d/"context",d/"expected")
    r["report"]={k:v[k] for k in ("status","executed","case_counts","failures","missing","skips","errors") if k in v}
    r["executions"]=[]
    for line in clean.read_text().splitlines():
        event=json.loads(line)
        if "testResult" not in event: continue
        row=event["testResult"]
        meta=row.get("executionInfo",{})
        r["executions"].append({"owner":event["id"]["testResult"]["label"],"remote":meta.get("strategy")=="remote",
                                "cached":bool(row.get("cachedLocally") or meta.get("cachedRemotely"))})
    records=[]
    for f in m["reports"]:
        if f["name"]!="test.xml": continue
        for case in ET.parse(d/f["path"]).iter("testcase"):
            records.append({"name":case.get("name"),"owner":f["identity"]["label"],
                            "result":next((c.tag for c in case if c.tag in ("skipped","failure","error")),"pass")})
    r["cases"]=records
    r["test_diagnostics"] = {
        f["identity"]["label"]: [line[:1500] for line in (d/f["path"]).read_text(errors="replace").splitlines()
                                  if re.search(r"[.]go:\d+|^amd64:|^arm64:|findings", line)][:300]
        for f in m["reports"] if f["name"] == "test.log"
    }
    found={(row["owner"],row["name"]) for row in records if row["result"]=="pass"}
    r["missing_required"]=sorted(required-found)
    ran={e["owner"] for e in r["executions"] if e["remote"] and not e["cached"]}
    if negative:
        logs = "\n".join((d/f["path"]).read_text(errors="replace") for f in m["reports"] if f["name"] == "test.log")
        diagnostics = [line for line in logs.splitlines() if "checklocks" in line and "fd_table.go:" in line]
        required_diagnostics = {}
        for arch in ("amd64", "arm64"):
            lines = [line for line in diagnostics if "GOOARCH=" + arch in line]
            required_diagnostics[arch] = {
                "setter": any("to call set, but not held" in line for line in lines),
                "bitmap": any("invalid field access" in line and "fdBitmap" in line for line in lines),
                "iterator": any("to call forEach, but not held" in line for line in lines),
                "remove_if_iterator": any("fd_table.go:494:" in line and "to call forEach, but not held" in line for line in lines),
            }
        r["negative_control"] = required_diagnostics
        r["status"] = "EXPECTED_FAILURE" if code and v["status"] == "FAIL" and not v.get("missing") and ran == set(owners) and all(all(checks.values()) for checks in required_diagnostics.values()) else "FAIL"
    else:
        r["status"]="PASS" if v["status"]=="PASS" and ran==set(owners) and not r["missing_required"] and not code else "FAIL"
except Exception as exc:
    r["collector_error"]=f"{type(exc).__name__}: {str(exc)[:200]}"
    if r.get("native_exit",0): r["status"]="FAIL"
finally:
    r["verified_at"]=now()
    save("result.json",r)
sys.exit(int(r["status"] not in ("PASS", "EXPECTED_FAILURE")))
