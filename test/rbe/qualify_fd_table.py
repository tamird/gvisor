"""Run selected package owners and verify required cases from their original XML."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import traceback
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

import qualification_report as report

pr, architecture, source, destination = sys.argv[1:]
assert pr == "race" and architecture == "amd64"
owner = "//pkg/sentry/kernel:kernel_test"
wanted = "TestPIDFDGetFDConcurrentUnshare"
owners = [owner]
required = {(owner, wanted)}
d = Path(destination)
d.mkdir(exist_ok=True)
def now():
    return datetime.now(timezone.utc).isoformat()
def git(*args):
    return subprocess.check_output(["git", *args])
def save(name, value):
    (d/name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")

# Adapt the failed-console/action projection from the native buffer race run.
# Keep just diagnostic lines, even for stderr belonging to a failed action.
diagnostic = re.compile(r"^(?:ERROR:|FATAL:|WARNING: Download|Error in |\s*(?:compilepkg|compile|link|cgo|runtime/cgo|clang|cc|ld.lld): )|(?:\.go|\.bzl|\.[ch]c?|BUILD(?:\.bazel)?):[0-9]+:|\b(?:undefined|error):")
forbidden = re.compile(r"(?i)authorization|credential|password|secret|access.token|api.key|gh[pousr]_|github_pat_|private key|bearer\s")
url = re.compile(r"(?:[a-z][a-z0-9+.-]*://|git@)\S+", re.I)
shell = re.compile(r"(?:\b[a-zA-Z_]\w*=|\$\{|\$\(|--[A-Za-z]|(?:\bexec env|/bin/(?:ba)?sh -c)\s)")

def diagnostic_lines(text):
    lines = []
    for original in text.splitlines():
        if not diagnostic.search(original) or forbidden.search(original):
            continue
        line = url.sub("<url>", original)
        # Bazel can append the full command after this summary.
        line = re.sub(r"(error executing \w+ command)\b.*", r"\1", line)
        if shell.search(line) or not line.isprintable():
            continue
        for path, replacement in ((Path.cwd(), "<workspace>"), (d.parent, "<scratch>"), (Path.home(), "<home>")):
            line = line.replace(str(path), replacement)
        lines.append(line[:500])
        if len(lines) == 6:
            break
    return lines

r = {"pr": pr, "architecture": architecture, "source": source, "expected": owners, "required_cases": sorted(required), "status": "INCOMPLETE"}
try:
    r["runner_os"] = platform.freedesktop_os_release()["VERSION_ID"]
    assert r["runner_os"] == "24.04" and os.uname().machine == "x86_64"
    stat = os.statvfs(d)
    r["disk_free_bytes_at_start"] = stat.f_bavail * stat.f_frsize
    r["memory_bytes"] = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    r["logical_cpus"] = len(os.sched_getaffinity(0))
    r["started_at"] = now()
    save("result.json", r)
    assert r["disk_free_bytes_at_start"] >= 12 * 1024**3 and r["memory_bytes"] >= 8 * 1024**3 and r["logical_cpus"] >= 4
    assert git("rev-parse", "HEAD").decode().strip() == source
    assert source == "bd8c827e2eaad36ffba2c4abcca0faf40ae122f3", source
    assert git("rev-parse", "HEAD^").decode().strip() == "caa2f641cb64ef4d22c5b9269d2d54acd0317f81"
    r["vdso_cc_product"] = "1d73cd49653e6b8c4c266168173b991c0e5fb4ae"
    r["vdso_product"] = "cf2264788a7cfe5302a96311d314bf715d50de9a"
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
    bazel = ["bazel", "--nosystem_rc", "--nohome_rc", "--bazelrc=/dev/null", "--host_jvm_args=-Xmx3g"]
    options = ["--config=race", "--extra_toolchains=@llvm//toolchain:linux_x86_64_to_linux_aarch64"]
    args = ["test", *options, "--compilation_mode=opt", "--jobs=4", "--local_resources=memory=8192",
            "--remote_cache=", "--remote_executor=", "--bes_backend=", "--spawn_strategy=local", "--strategy=TestRunner=local",
            "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
            "--test_filter=^" + wanted + "$", "--test_timeout=300", "--nocache_test_results", "--test_sharding_strategy=disabled", "--runs_per_test=1", "--flaky_test_attempts=1",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--build_event_json_file=" + str(d/"events.raw"),
            "--build_event_json_file_path_conversion=false",
            *owners]
    r["arguments"] = [arg for arg in args if not arg.startswith("--build_event_json_file=")]
    r["timeout_seconds"] = 5400
    save("result.json", r)
    with (d/"command.log").open("wb") as l:
        code = subprocess.run(["timeout", "-k", "20", str(r["timeout_seconds"]), *bazel, *args], stdout=l, stderr=subprocess.STDOUT).returncode
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
    r["console_sha256"] = hashlib.sha256((d/"command.log").read_bytes()).hexdigest()
    r["hints"] = [h for h in ("no space left on device", "SSLHandshakeException", "No space left", "failed to fetch", "no such target", "Permission denied", "not found", "signal: killed") if h.lower() in log.lower()]
    if code:
        r["diagnostics"] = diagnostic_lines(log)
        r["diagnostic_classes"] = [term for term in ("missing strict dependencies", "undefined:", "no such file or directory", "no such package", "Error downloading", "race detected", "could not import", "incompatible", "out of memory", "no usable", "no matching toolchains", "absolute path inclusion", "runtime/cgo", "cc: no such file or directory", "failed to download", "could not resolve", "out of disk space") if term.lower() in log.lower()]
        save("result.json", r)
    clean=d/"events.jsonl"
    outroot=Path(subprocess.check_output(bazel+["info","output_base"],stderr=subprocess.DEVNULL,text=True).strip())
    if code and (d/"events.raw").is_file():
        r["failed_actions"] = []
        r["test_results_in_bep"] = 0
        for line in (d/"events.raw").open():
            event = json.loads(line)
            r["test_results_in_bep"] += int("testResult" in event.get("id", {}))
            action = event.get("action")
            if not action or action.get("success") or len(r["failed_actions"]) == 2:
                continue
            mnemonic = action.get("type", "")
            item = {"type": mnemonic if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", mnemonic) else "unknown",
                    "exit": action.get("exitCode"), "stderr": "UNAVAILABLE"}
            r["failed_actions"].append(item)
            uri = urlsplit(action.get("stderr", {}).get("uri", ""))
            if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
                continue
            path = Path(unquote(uri.path)).resolve()
            if not path.is_relative_to(outroot.resolve()) or not path.is_file() or path.stat().st_size > 65536:
                continue
            data = path.read_bytes()
            item["stderr"] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                              "diagnostics": diagnostic_lines(data.decode(errors="replace"))}
        save("result.json", r)
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
                                "strategy":meta.get("strategy"),
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
    assert run["owner"] == owner and run["strategy"] == "local" and not run["cached"] and run["status"] == "PASSED"
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
    r["verifier_lines"]=[frame.lineno for frame in traceback.extract_tb(exc.__traceback__) if Path(frame.filename).name == "qualify_fd_table.py"]
    if r.get("native_exit",0): r["status"]="FAIL"
finally:
    r["verified_at"]=now()
    save("result.json",r)
sys.exit(int(r["status"] != "PASS"))
