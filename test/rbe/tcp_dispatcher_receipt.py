"""Run selected package owners and verify required cases from their original XML."""
from collections import Counter
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

phase, source, destination = sys.argv[1:]
owner = "//pkg/tcpip/transport/tcp:tcp_test"
analyzer = "//pkg/tcpip/transport/tcp:tcp_nogo"
assert phase in ("candidate", "published")
owners = [owner, analyzer] if phase == "candidate" else [owner]
wanted = "TestProcessorRetriesUnlockedEndpoint"
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

r = {"issue": 16878, "architecture": "amd64", "source": source, "expected": owners,
     "permanent_case": wanted, "phase": phase, "status": "INCOMPLETE"}
try:
    r["runner_os"] = platform.freedesktop_os_release()["VERSION_ID"]
    assert r["runner_os"] == "22.04" and os.uname().machine == "x86_64"
    stat = os.statvfs(d)
    r["disk_free_bytes_at_start"] = stat.f_bavail * stat.f_frsize
    r["memory_bytes"] = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    r["logical_cpus"] = len(os.sched_getaffinity(0))
    r["started_at"] = now()
    save("result.json", r)
    assert r["disk_free_bytes_at_start"] >= 12 * 1024**3 and r["memory_bytes"] >= 6 * 1024**3 and r["logical_cpus"] >= 2
    product = "ba2c7a721f72098af19cce916ccaf1ddc5d6cf8d"
    control = "1b59fc576a7ebfaaefdc24280422fbed52f405ee"
    base = "90d4e2253f004cdc73a35dbc4050eab3e9a8495d"
    package = "pkg/tcpip/transport/tcp/"
    dispatcher = package + "dispatcher.go"
    test = package + "endpoint_test.go"
    assert git("rev-parse", "HEAD").decode().strip() == source == (product if phase == "candidate" else control)
    assert git("rev-parse", product+"^").decode().strip() == base
    assert git("rev-parse", control+"^").decode().strip() == product
    assert git("diff", "--name-only", base, product).decode().splitlines() == [dispatcher, test]
    assert git("diff", "--name-only", product, control).decode().splitlines() == [dispatcher]
    assert git("rev-parse", control+":"+dispatcher).decode().strip() == "20eeb148c65d41011455ed7ce41f8e17f8b3aead"
    assert git("rev-parse", source+":"+test).decode().strip() == "29d79fef4a48d1c881ca0d4d8f89132c23e362cf"
    assert git("rev-parse", source+":"+package+"BUILD").decode().strip() == "3b029530a3e7cbaa0e7d6cde73684ff519b66f25"
    assert git("show", source+":images/default/bazelversion").decode().strip() == "9.3.1"
    build = git("show", source+":"+package+"BUILD").decode()
    srcs = re.search(r'go_test\(\s*name = "tcp_test",.*?srcs = \[(.*?)\]', build, re.S).group(1)
    files = re.findall(r'"([a-z_]+_test.go)"', srcs)
    assert len(files) == 8
    expected_top = []
    for filename in files:
        assert git("show", product+":"+package+filename) == git("show", control+":"+package+filename)
        expected_top.extend(re.findall(r'^func (Test\w+)\(t \*testing.T\)', git("show", source+":"+package+filename).decode(), re.M))
    assert wanted in expected_top and len(expected_top) == len(set(expected_top))
    r.update(product=product, base=base, control=control,
             test_blob="29d79fef4a48d1c881ca0d4d8f89132c23e362cf",
             expected_top_level_cases=sorted(expected_top) if phase == "candidate" else [wanted],
             control_scope="published queue implementation with the identical permanent test",
             analysis_architectures=["amd64", "arm64"] if phase == "candidate" else [])
    r["tree"] = git("rev-parse", "HEAD^{tree}").decode().strip()
    (d/"before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    assert not (d/"before").read_bytes()
    (d/"commit").write_bytes(git("cat-file", "commit", source))
    bazel = ["bazel", "--nosystem_rc", "--nohome_rc", "--bazelrc=/dev/null", "--host_jvm_args=-Xmx3g"]
    options = ["--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
               "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox", "--build_event_publish_all_actions"]
    if phase == "published":
        options.append("--test_filter=^" + wanted + "$")
    args = ["test", *options, "--compilation_mode=opt", "--jobs=4", "--local_resources=memory=8192",
            "--remote_cache=", "--remote_executor=", "--bes_backend=", "--spawn_strategy=linux-sandbox", "--strategy=TestRunner=linux-sandbox", "--sandbox_default_allow_network=false",
            "--test_timeout=120", "--test_arg=-test.v", "--nocache_test_results", "--test_sharding_strategy=disabled", "--runs_per_test=1", "--flaky_test_attempts=1",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--build_event_json_file=" + str(d/"events.raw"),
            "--build_event_json_file_path_conversion=false",
            *owners]
    r["arguments"] = [arg for arg in args if not arg.startswith("--build_event_json_file=")]
    r["timeout_seconds"] = 3000 if phase == "candidate" else 900
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
        r["no_usable_strategy_mnemonics"] = [
            name for name in ("GoStandardLibraryAnalysis", "GoStaticAnalysis", "GoStdlib", "GoStdlibList", "TestRunner")
            if re.search(r"\b" + name + r" spawn[^\n]* cannot be executed with any of the available strategies", log)
        ]
        r["diagnostic_classes"] = [term for term in ("missing strict dependencies", "undefined:", "no such file or directory", "no such package", "Error downloading", "race detected", "could not import", "incompatible", "out of memory", "no usable", "no matching toolchains", "absolute path inclusion", "runtime/cgo", "cc: no such file or directory", "failed to download", "could not resolve", "out of disk space") if term.lower() in log.lower()]
        save("result.json", r)
    actions = []
    if (d/"events.raw").is_file():
        for line in (d/"events.raw").open():
            event = json.loads(line)
            action = event.get("action", {})
            name = action.get("type")
            if name not in {"GoStdlib","GoStdlibList","GoStandardLibraryAnalysis"}:
                continue
            identity = event.get("id", {}).get("actionCompleted", {})
            row = {"type":name, "success":bool(action.get("success")), "exit":action.get("exitCode",0),
                   "stdlib_nogo":identity.get("label") == "//tools/nogo:stdlib"}
            if row["stdlib_nogo"]:
                details = action.get("strategyDetails", [])
                row["runners"] = sorted({item["runner"] for item in details if item.get("runner") in ("linux-sandbox", "local", "processwrapper-sandbox")})
                for key in ("startTime", "endTime"):
                    timestamp = action.get(key, "")
                    if re.fullmatch(r"[0-9T:.Z-]{15,35}", timestamp):
                        row[key] = timestamp
                row["no_usable_strategy"] = action.get("failureDetail", {}).get("spawn", {}).get("code") == "NO_USABLE_STRATEGY_FOUND"
            actions.append(row)
    r["stdlib_actions"] = actions
    configurations = {}
    for line in (d/"events.raw").open():
        event = json.loads(line)
        if "configuration" in event and "configuration" in event.get("id", {}):
            configurations[event["id"]["configuration"]["id"]] = event["configuration"].get("cpu")
    save("result.json",r)
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
                                "cpu":configurations.get(event["id"]["testResult"].get("configuration", {}).get("id")),
                                "cached":bool(row.get("cachedLocally") or meta.get("cachedRemotely"))})
    records=[]
    for f in m["reports"]:
        if f["name"]!="test.xml": continue
        for case in ET.parse(d/f["path"]).iter("testcase"):
            name = case.get("name", "")
            assert name.isprintable() and len(name) <= 200
            records.append({"name":name, "owner":f["identity"]["label"],
                            "result":"skipped" if case.get("status")=="notrun" or case.get("result")=="suppressed" else next((c.tag for c in case if c.tag in ("skipped","failure","error")),"pass")})
    r["cases"] = records
    r["observed_case_counts"] = dict(Counter(row["result"] for row in records))
    save("result.json", r)
    logpath, = (d/f["path"] for f in m["reports"] if f["name"] == "test.log" and f["identity"]["label"] == owner)
    logtext = logpath.read_text(errors="replace").replace("\x16", "")
    r["boundary_failure"] = "after listener unlocked: yielded=true, processed=0, want false, 1" in logtext
    r["analyzer_sections"] = []
    if phase == "candidate":
        paths = [d/f["path"] for f in m["reports"] if f["name"] == "test.log" and f["identity"]["label"] == analyzer]
        if len(paths) == 1:
            r["analyzer_sections"] = re.findall(r"^(amd64|arm64)\.\.\.(PASS|FAIL)$", paths[0].read_text(errors="replace"), re.M)
    save("result.json", r)
    assert not m["errors"] and len(r["reports"]) == 2 * len(owners)
    assert Counter(run["owner"] for run in r["executions"]) == Counter(owners)
    for run in r["executions"]:
        assert run["strategy"] == "linux-sandbox" and run["cpu"] == "k8" and not run["cached"]
        assert all(run["identity"].get(key) == 1 for key in ("run", "attempt", "shard"))
        assert sorted(f["name"] for f in r["reports"] if f["identity"] == run["identity"]) == ["test.log", "test.xml"]
    unit = [row for row in records if row["owner"] == owner]
    top = [row["name"] for row in unit if "/" not in row["name"]]
    assert Counter(top) == Counter(r["expected_top_level_cases"])
    assert len({(row["owner"], row["name"]) for row in records}) == len(records)
    if phase == "published":
        assert code == 3 and v["status"] == "FAIL" and r["executions"][0]["status"] == "FAILED"
        assert not v.get("errors") and not v.get("missing") and not v.get("skips")
        assert len(records) == 1 and records[0]["result"] == "failure" and r["boundary_failure"]
        r["disposition"] = "EXPECTED_YIELD_AFTER_UNLOCK"
    else:
        assert code == 0 and v["status"] == "PASS" and all(run["status"] == "PASSED" for run in r["executions"])
        assert not any(r["report_problem_counts"].values()) and all(row["result"] == "pass" for row in records)
        assert sorted(r["analyzer_sections"]) == [("amd64", "PASS"), ("arm64", "PASS")]
        assert not r["boundary_failure"]
        r["disposition"] = "FULL_NATIVE_TCP_TEST_AND_TWO_ARCH_ANALYSIS_PASSED"
    r["status"] = "PASS"
except (AssertionError, KeyError, ValueError, OSError, RuntimeError, subprocess.SubprocessError, ET.ParseError) as exc:
    r["collector_error"] = type(exc).__name__
    r["collector_line"] = traceback.extract_tb(exc.__traceback__)[-1].lineno
finally:
    r["verified_at"] = now()
    save("result.json", r)
sys.exit(int(r["status"] != "PASS"))
