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
assert pr == "rack-probe-flight"
case_owners = {
    "//pkg/tcpip/transport/tcp:tcp_nogo": [],
    "//pkg/tcpip/transport/tcp:tcp_test": ["TestRACKPriorTailLoss", "TestRACKPriorTailDelivery"],
    "//pkg/tcpip/transport/tcp/test/e2e:tcp_rack_test": ["TestRACKRepairsLostBatch", "TestRACKCountsTailProbe", "TestRACKCountsTailProbeBeforeACK"],
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
    assert git("rev-parse", "HEAD^").decode().strip() == "7859018167653b4fb3f762878cba599c4d1ec5c7"
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
    r["failure_excerpt"] = [line[:500] for line in log.splitlines() if re.match(r"^pkg/tcpip/transport/tcp/[a-zA-Z0-9_/.-]+:[0-9]+:",line)][:24] if code else []
    r["hints"] = [h for h in ("RESOURCE_EXHAUSTED", "no such target", "Permission denied", "not found", "signal: killed") if h.lower() in log.lower()]
    clean=d/"events.jsonl"
    outroot=Path(subprocess.check_output(bazel+["info","output_base"],stderr=subprocess.DEVNULL,text=True).strip())
    m=report.capture(d/"events.raw",clean,outroot,d/"reports")
    (d/"capture").write_text(str(int(bool(m["errors"])))+"\n")
    v=report.verdict(clean,report.manifest_path(clean),d/"context",d/"expected")
    r["report"]={k:v[k] for k in ("status","executed","case_counts","failures","missing","skips","errors") if k in v}
    r["flight"]=[]
    r["reports"]=[]
    for f in m["reports"]:
        if f["name"] not in {"test.xml", "test.log"}: continue
        data=(d/f["path"]).read_bytes()
        import hashlib
        r["reports"].append({"owner":f["identity"]["label"],"name":f["name"],"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()})
        if f["name"]!="test.log" or f["identity"]["label"]!="//pkg/tcpip/transport/tcp/test/e2e:tcp_rack_test": continue
        for line in data.decode(errors="replace").splitlines():
            if "FLIGHT " in line:
                event=line.partition("FLIGHT ")[2]
                if re.fullmatch(r"[0-9A-Za-z ?.,;=()\[\)-]{1,160}",event):
                    r["flight"].append(event)
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
    found={(row["owner"],row["name"]) for row in records if row["result"]=="pass"}
    r["missing_required"]=sorted(required-found)
    ran={e["owner"] for e in r["executions"] if e["remote"] and not e["cached"]}
    r["status"]="PASS" if v["status"]=="PASS" and ran==set(owners) and not r["missing_required"] and not code else "FAIL"
except Exception as exc:
    r["collector_error"]=f"{type(exc).__name__}: {str(exc)[:200]}"
    if r.get("native_exit",0): r["status"]="FAIL"
finally:
    r["verified_at"]=now()
    save("result.json",r)
sys.exit(int(r["status"]!="PASS"))
