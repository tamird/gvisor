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
assert pr == "tcp-error-locks"
case_owners = {"//pkg/tcpip/transport/tcp:tcp_nogo": []}
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
    assert git("rev-parse", "HEAD^").decode().strip() == '2d6624b7225ff19bbada4fe2fa6724fdb9109cbb'
    assert git("diff", "--name-only", "HEAD^", "HEAD").decode().splitlines() == ["pkg/tcpip/transport/tcp/endpoint.go"]
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
    r["reports"] = []
    r["diagnostics"] = []
    for f in m["reports"]:
        if f["name"] not in ("test.log", "test.xml"):
            continue
        import hashlib
        body = (d/f["path"]).read_bytes()
        r["reports"].append({"name": f["name"], "owner": f["identity"]["label"],
                             "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        if f["name"] == "test.xml":
            r["cases"] = [
                {"name": case.attrib["name"], "result": next((x.tag for x in case if x.tag in ("failure","skipped","error")),"pass")}
                for case in ET.fromstring(body).iter("testcase")]
            continue
        for line in body.decode().splitlines():
            diagnostic = re.search(r"checklocks: pkg/tcpip/transport/tcp/endpoint[.]go:[0-9]+:[0-9]+: invalid field access, .{1,340} [(]GOO?ARCH=(amd64|arm64), GOOS=linux[)]$",line)
            if diagnostic:
                r["diagnostics"].append(diagnostic[0])
    expected = {"hardError": "mu", "lastError": "lastErrorMu"}
    r["missing_fields"] = [
        [arch, field] for arch in ("amd64","arm64") for field, guard in expected.items()
        if not any("invalid field access, " + guard + " " in line and "accessing " + field + " " in line and "ARCH=" + arch + "," in line
                   for line in r["diagnostics"])]
    r["control_observed"] = bool(code == 3 and v["status"] == "FAIL" and not r["missing_fields"] and len(r["executions"]) == 1
                                 and all(w["remote"] and not w["cached"] for w in r["executions"]))
    r["status"] = v["status"]
except Exception as exc:
    r["collector_error"]=f"{type(exc).__name__}: {str(exc)[:200]}"
    if r.get("native_exit",0): r["status"]="FAIL"
finally:
    r["verified_at"]=now()
    save("result.json",r)
sys.exit(int(r["status"]!="PASS"))
