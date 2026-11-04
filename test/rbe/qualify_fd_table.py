"""Run selected package owners and verify required cases from their original XML."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

import qualification_report as report

pr, architecture, source, rc, destination = sys.argv[1:]
assert pr == "race" and architecture == "amd64"
owner = "//pkg/sentry/kernel:kernel_test"
wanted = "TestPIDFDGetFDConcurrentUnshare"
owners = [owner]
required = {(owner, wanted)}
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
    assert source == "97bfc4ae73a4e74751f4db335918970889807bb6", source
    r["product"] = "8cdb6eb0da66ecf93b0a5e32a2ee970016833a5a"
    r["fixture_donor"] = "11e80af23adffb0bc7a17ff57b2f2d4b212dc64d"
    data = Path("pkg/sentry/kernel/fd_table_test.go").read_bytes()
    start = data.index(b"func TestPIDFDGetFDConcurrentUnshare(")
    r["fixture_sha256"] = hashlib.sha256(data[start:data.index(b"\n}", start)+2]).hexdigest()
    assert r["fixture_sha256"] == "cde7f100d3d2e4adedf3f75666372d56ea0002654a2020fbb54d1df46b0e7b42"

    r["tree"] = git("rev-parse", "HEAD^{tree}").decode().strip()
    (d/"before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    assert not (d/"before").read_bytes()
    (d/"commit").write_bytes(git("cat-file", "commit", source))
    bazel = ["bazel", "--bazelrc=" + rc]
    options = ["--config=race", "--extra_toolchains=@llvm//toolchain:linux_x86_64_to_linux_aarch64"]
    args = ["test", *options, "--strategy=TestRunner=remote", "--modify_execution_info=^TestRunner$=+no-remote-cache",
            "--test_filter=^" + wanted + "$", "--test_timeout=150", "--remote_download_outputs=toplevel", "--nocache_test_results", "--test_sharding_strategy=disabled", "--runs_per_test=1", "--flaky_test_attempts=1",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--build_event_json_file=" + str(d/"events.raw"),
            "--build_event_json_file_path_conversion=false",
            "--remote_download_regex=.*[/](test[.](log|xml)|test[.]outputs/.*)$", *owners]
    with (d/"command.log").open("wb") as l:
        code = subprocess.run(["timeout", "-k", "20", "2400", *bazel, *args], stdout=l, stderr=subprocess.STDOUT).returncode
    r.update(native_exit=code, execution_finished_at=now())
    if code:
        r["status"] = "FAIL"
    save("result.json", r)
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
    if code:
        r["diagnostic_classes"] = [term for term in ("missing strict dependencies", "undefined:", "no such file or directory", "no such package", "Error downloading", "race detected", "could not import", "incompatible", "out of memory", "no usable", "no matching toolchains") if term.lower() in log.lower()]
        save("result.json", r)
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
    r["race_config"] = "--config=race" in args
    r["bazelrc_blob"] = git("rev-parse", "HEAD:.bazelrc").decode().strip()
    assert "test:race --@io_bazel_rules_go//go/config:race --@io_bazel_rules_go//go/config:pure=false" in Path(".bazelrc").read_text().splitlines()
    assert code == 0 and v["status"] == "PASS" and not any(r["report_problem_counts"].values())
    run, = r["executions"]
    assert run["owner"] == owner and run["remote"] and not run["cached"] and run["status"] == "PASSED"
    assert records == [{"name":wanted,"owner":owner,"result":"pass"}]
    assert {f["name"] for f in r["reports"]} == {"test.xml","test.log"}
    assert all(f["identity"] == run["identity"] for f in r["reports"])
    save("result.json", r)
    # Identify the executable from this invocation's targetCompleted event,
    # not from an independently configured build or an old bazel-bin path.
    sets = {}
    output_sets = []
    with (d/"events.raw").open() as stream:
        for line in stream:
            event = json.loads(line)
            ident = event.get("id", {})
            if "namedSet" in ident:
                fs = event["namedSetOfFiles"]
                sets[ident["namedSet"]["id"]] = (fs.get("files", []), fs.get("fileSets", []))
            target = ident.get("targetCompleted", {})
            if target.get("label") == owner and target.get("configuration") == run["identity"]["configuration"] and not target.get("aspect"):
                assert event["completed"]["success"]
                for group in event["completed"]["outputGroup"]:
                    if group["name"] == "default":
                        output_sets.extend(group.get("fileSets", []))
    assert output_sets
    files = []
    seen = set()
    while output_sets:
        ident = output_sets.pop()["id"]
        if ident not in seen:
            seen.add(ident)
            group, children = sets[ident]
            files.extend(group)
            output_sets.extend(children)
    exe, = [f for f in files if Path(f.get("name", "")).name == "kernel_test"]
    uri = urlsplit(exe["uri"])
    assert (uri.scheme, uri.netloc) == ("file", "")
    binary = Path(unquote(uri.path)).resolve()
    binary.relative_to(outroot.resolve())
    output = exe["name"]
    assert binary.is_file() and 0 < binary.stat().st_size < 512 * 1024**2
    data = binary.read_bytes()
    assert data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[18:20], "little") == 62
    markers = {name:(b"\0"+name.encode()+b"\0") in data for name in ("runtime.raceinit", "runtime.racefini")}
    r["binary"] = {"output":output, "sha256":hashlib.sha256(data).hexdigest(), "bytes":len(data), "elf_machine":62,
                   "race_runtime_functions":markers}
    if exe.get("digest"):
        assert exe["digest"] == r["binary"]["sha256"]
    assert all(markers.values())
    r["status"] = "PASS"
except (AssertionError, KeyError, OSError, ValueError, ET.ParseError, subprocess.SubprocessError) as exc:
    r["collector_error"]=type(exc).__name__
    if r.get("native_exit",0): r["status"]="FAIL"
finally:
    r["verified_at"]=now()
    save("result.json",r)
sys.exit(int(r["status"] != "PASS"))
