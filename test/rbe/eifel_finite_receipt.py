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

phase, source, destination = sys.argv[1:]
owner = "//pkg/tcpip/transport/tcp/test/e2e:tcp_sack_test"
owners = [owner]
wanted = "TestEifelFiniteApplicationFlight"
required = {(owner, wanted + suffix) for suffix in ("", "/original_ack", "/repair_ack")}
assert phase in ("submitted", "flight_control")
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

r = {"pr": 17022, "architecture": "amd64", "source": source, "expected": owners,
     "required_cases": sorted(required), "phase": phase, "status": "INCOMPLETE"}
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
    submitted = "963e83a9586fe9ea5331bd4e3e30d53d45386d66"
    fixture = "36059e7fc549757067a24b8ca5fc35f2b6e6949e"
    control = "c5dc2ab1b2c82f7e6d99a01f3614b7e55fe5d5ed"
    path = "pkg/tcpip/transport/tcp/test/e2e/tcp_sack_test.go"
    sender = "pkg/tcpip/transport/tcp/snd.go"
    assert git("rev-parse", "HEAD").decode().strip() == source == (fixture if phase == "submitted" else control)
    assert git("rev-parse", fixture+"^").decode().strip() == submitted
    assert git("rev-parse", control+"^").decode().strip() == fixture
    assert git("diff", "--name-only", submitted, fixture).decode().splitlines() == [path]
    assert git("diff", "--name-only", fixture, control).decode().splitlines() == [sender]
    assert git("show", fixture+":"+sender).replace(b"s.pipePrev = s.SndCwnd", b"s.pipePrev = s.Outstanding") == git("show", control+":"+sender)
    assert git("rev-parse", source+":"+path).decode().strip() == "f13c3af1635f875d1b937727f85abe0d5834b259"
    assert git("show", source+":images/default/bazelversion").decode().strip() == "9.3.1"
    r.update(submitted=submitted, fixture=fixture, control=control,
             test_blob="f13c3af1635f875d1b937727f85abe0d5834b259", control_scope="diagnostic capture assignment only; no fast-recovery conclusion")
    r["tree"] = git("rev-parse", "HEAD^{tree}").decode().strip()
    (d/"before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    assert not (d/"before").read_bytes()
    (d/"commit").write_bytes(git("cat-file", "commit", source))
    bazel = ["bazel", "--nosystem_rc", "--nohome_rc", "--bazelrc=/dev/null", "--host_jvm_args=-Xmx3g"]
    options = ["--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
               "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox", "--build_event_publish_all_actions",
               "--test_filter=^TestEifelFiniteApplicationFlight$"]
    args = ["test", *options, "--compilation_mode=opt", "--jobs=4", "--local_resources=memory=8192",
            "--remote_cache=", "--remote_executor=", "--bes_backend=", "--spawn_strategy=linux-sandbox", "--strategy=TestRunner=linux-sandbox", "--sandbox_default_allow_network=false",
            "--test_timeout=120", "--test_arg=-test.v", "--nocache_test_results", "--test_sharding_strategy=disabled", "--runs_per_test=1", "--flaky_test_attempts=1",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--build_event_json_file=" + str(d/"events.raw"),
            "--build_event_json_file_path_conversion=false",
            *owners]
    r["arguments"] = [arg for arg in args if not arg.startswith("--build_event_json_file=")]
    r["timeout_seconds"] = 3000 if phase == "submitted" else 900
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
            records.append({"name":case.get("name") if (f["identity"]["label"],case.get("name")) in required else "<unexpected>","owner":f["identity"]["label"],
                            "result":"skipped" if case.get("status")=="notrun" or case.get("result")=="suppressed" else next((c.tag for c in case if c.tag in ("skipped","failure","error")),"pass")})
    r["cases"]=records
    save("result.json", r)
    logpath, = (d/f["path"] for f in m["reports"] if f["name"] == "test.log" and f["identity"]["label"] == owner)
    logtext = logpath.read_text(errors="replace").replace("\x16", "")
    r["fixture_locations"] = sorted(set(re.findall(r"\btcp_sack_test\.go:[0-9]{1,4}\b", logtext)))[:32]
    r["precondition_diagnostics"] = re.findall(
        r"tcp_sack_test\.go:[0-9]+: ((?:finite application-limited threshold|precondition:|genuine recovery:|window changed|wrong recovery path:|missing real timeout:|unexpected RTO|flight not drained:|write =|offset [0-9]+:|packet budget exceeded|ACK was not processed)[^\n]{0,400})", logtext)[:8]
    observation = r"EIFEL_FINITE case=(original_ack|repair_ack) pre_cwnd=(\d+) pre_ssthresh=(\d+) flight=(\d+) rto_ms=(\d+) timeout_cwnd=(\d+) timeout_ssthresh=(\d+) original=(\d+) repair=(\d+) echo=(\d+) post_cwnd=(\d+) post_ssthresh=(\d+) timeouts=(\d+) spurious=(\d+)"
    matches = re.findall(observation, logtext)
    keys = ("pre_cwnd", "pre_ssthresh", "flight", "rto_ms", "timeout_cwnd", "timeout_ssthresh", "original", "repair", "echo", "post_cwnd", "post_ssthresh", "timeouts", "spurious")
    r["observations"] = {name:dict(zip(keys,map(int,values))) for name,*values in matches}
    save("result.json", r)
    assert not m["errors"] and len(r["reports"]) == 2
    assert len(r["executions"]) == 1
    run, = r["executions"]
    assert run["owner"] == owner and run["strategy"] == "linux-sandbox" and run["cpu"] == "k8" and not run["cached"]
    assert sorted(f["name"] for f in r["reports"] if f["identity"] == run["identity"]) == ["test.log", "test.xml"]
    assert len(records) == len(required) and {(row["owner"],row["name"]) for row in records} == required
    assert len(matches) == 2 and set(r["observations"]) == {"original_ack", "repair_ack"}
    for name, observation in r["observations"].items():
        assert [observation[k] for k in ("pre_cwnd", "pre_ssthresh", "flight", "timeouts")] == [10, 2, 1, 2]
        assert 1 <= observation["rto_ms"] <= 60000
        assert [observation[k] for k in ("timeout_cwnd", "timeout_ssthresh")] == [1, 2]
        assert 0 < observation["original"] < observation["repair"]
        assert observation["echo"] == observation["original" if name == "original_ack" else "repair"]
        assert observation["spurious"] == int(name == "original_ack")
        assert observation["post_ssthresh"] == (10 if phase == "submitted" and name == "original_ack" else 2)
    if phase == "submitted":
        assert code == 3 and v["status"] == "FAIL" and run["status"] == "FAILED"
        assert not v.get("errors") and not v.get("missing") and not v.get("skips")
        assert {row["name"]:row["result"] for row in records} == {wanted:"failure", wanted+"/original_ack":"failure", wanted+"/repair_ack":"pass"}
        assert "finite application-limited threshold = 10, want max(1,2)=2" in logtext
        r["disposition"] = "EXPECTED_FINITE_THRESHOLD_COUNTEREXAMPLE"
    else:
        assert code == 0 and v["status"] == "PASS" and run["status"] == "PASSED"
        assert not any(r["report_problem_counts"].values()) and all(row["result"] == "pass" for row in records)
        r["disposition"] = "FLIGHT_CAPTURE_CONTROL_PASSED"
    r["status"] = "PASS"
except (AssertionError, KeyError, ValueError, OSError, RuntimeError, subprocess.SubprocessError, ET.ParseError) as exc:
    r["collector_error"] = type(exc).__name__
    r["collector_line"] = traceback.extract_tb(exc.__traceback__)[-1].lineno
finally:
    r["verified_at"] = now()
    save("result.json", r)
sys.exit(int(r["status"] != "PASS"))
