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
owner = "//pkg/tcpip/transport/tcp/test/e2e:delayed_ack_qualification_test"
wanted = "TestDelayedACKGapQualification"
author = "//pkg/tcpip/transport/tcp/test/e2e:tcp_delayed_ack_test"
owners = [author, owner]
author_cases = ("TestDelayedACKDisabledByDefault", "TestImmediateACKWhenDisabled",
                "TestImmediateACKWhenNotInteractive", "TestDelayedACKTimeout",
                "TestDelayedACKSentWithData", "TestDelayedACKSecondSegment",
                "TestDelayedACKOutOfOrder")
required = {(owner, n) for n in (wanted, wanted+"/default", wanted+"/enabled")}
required.update((author, name) for name in author_cases)
restore_case = "TestPendingDelayedACKAcrossRestore"
if phase in ("stock", "fixed"):
    owner = author
    owners = [author]
    required = {(author, name) for name in (restore_case, "TestDelayedACKTimeout")}
elif phase != "gap":
    raise ValueError("unknown phase")
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

r = {"pr": 16953, "architecture": "amd64", "source": source, "expected": owners, "required_cases": sorted(required), "status": "INCOMPLETE"}
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
    assert git("rev-parse", "HEAD").decode().strip() == source
    submitted = "5e903e17232e9191f5c20fa48a9a3ab171826cb1"
    base = "6dcadf4a84a07f69e8eb5f1ea9d7b2903bd03bd7"
    directory = "pkg/tcpip/transport/tcp/test/e2e/"
    if phase == "gap":
        fixture = "8ceb0ea415f881eada665c0fb4229e351d668f46"
        assert source == fixture
        testfile = "tcp_delayed_ack_gap_test.go"
        checked = {testfile: "1607ed804020dd28c116a7e3248f6556132a4e49", "BUILD": "fc0e363fa47c97853f08caec223ba584b4988511"}
    else:
        fixture = "cbb089c143d33281025229a2a7cf1495b2dc675f"
        expected_source = fixture if phase == "stock" else "0bb42c582ef70adb7f3022d9cd55cdb489e09844"
        assert source == expected_source
        if phase == "fixed":
            assert git("rev-parse", "HEAD^").decode().strip() == fixture
            assert git("diff", "--name-only", fixture, source).decode().splitlines() == ["pkg/tcpip/transport/tcp/endpoint_state.go"]
        testfile = "tcp_delayed_ack_restore_test.go"
        checked = {testfile: "cc980c7a6b5310c20fba1d47078b3341de281c2c", "BUILD": "5969f112876bc62721a59c96236f0b4117cdcc2f"}
    checked["tcp_delayed_ack_test.go"] = "5212bd7070a3a36d1b295f63711f249de64a8c12"
    assert git("rev-parse", fixture+"^").decode().strip() == submitted
    assert git("rev-parse", submitted+"^").decode().strip() == base
    assert git("diff", "--name-only", submitted, fixture).decode().splitlines() == [directory+"BUILD", directory+testfile]
    for file, blob in checked.items():
        assert git("rev-parse", source+":"+directory+file).decode().strip() == blob
    r.update(submitted=submitted, master=base, test_sources=checked)
    r["tree"] = git("rev-parse", "HEAD^{tree}").decode().strip()
    (d/"before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    assert not (d/"before").read_bytes()
    (d/"commit").write_bytes(git("cat-file", "commit", source))
    bazel = ["bazel", "--nosystem_rc", "--nohome_rc", "--bazelrc=/dev/null", "--host_jvm_args=-Xmx3g"]
    r["phase"] = phase
    r["previous_gap_trace"] = {"submitted": "97785277ac1befb39123fbf5a8706793e5dc8eab",
                     "fixture": "b41d5a16e248cf2cc1f5055f5d3711a1a926e6de",
                     "artifact": 13332099611, "zip_sha256": "11080aab5e915d55aabd52460f36a500898e3ed637e9ec1cc60777700ff0e725",
                     "observed_ms": {"default": 0, "enabled": 40}}
    options = ["--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox", "--build_event_publish_all_actions"]
    if phase != "gap":
        options.append("--test_filter=^(TestDelayedACKTimeout|TestPendingDelayedACKAcrossRestore)$")
    args = ["test", *options, "--compilation_mode=opt", "--jobs=4", "--local_resources=memory=8192",
            "--remote_cache=", "--remote_executor=", "--bes_backend=", "--spawn_strategy=linux-sandbox", "--strategy=TestRunner=linux-sandbox", "--sandbox_default_allow_network=false",
            "--test_timeout=120", "--test_arg=-test.v", "--nocache_test_results", "--test_sharding_strategy=disabled", "--runs_per_test=1", "--flaky_test_attempts=1",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--build_event_json_file=" + str(d/"events.raw"),
            "--build_event_json_file_path_conversion=false",
            *owners]
    r["arguments"] = [arg for arg in args if not arg.startswith("--build_event_json_file=")]
    r["timeout_seconds"] = 3000 if phase == "gap" else 840
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
    actions = []
    if (d/"events.raw").is_file():
        for line in (d/"events.raw").open():
            event = json.loads(line)
            action = event.get("action", {})
            name = action.get("type")
            if name not in {"GoStdlib","GoStdlibList","GoStandardLibraryAnalysis"}:
                continue
            identity = event.get("id", {}).get("actionCompleted", {})
            actions.append({"type":name, "success":bool(action.get("success")), "exit":action.get("exitCode",0),
                            "stdlib_nogo":identity.get("label") == "//tools/nogo:stdlib"})
    r["stdlib_actions"] = actions
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
    logtext = logpath.read_text(errors="replace")
    assert not m["errors"] and len(r["reports"]) == 2 * len(owners)
    assert len(r["executions"]) == len(owners) and {run["owner"] for run in r["executions"]} == set(owners)
    for run in r["executions"]:
        assert run["strategy"] == "linux-sandbox" and not run["cached"]
        assert sorted(f["name"] for f in r["reports"] if f["identity"] == run["identity"]) == ["test.log", "test.xml"]
    assert len(records) == len(required) and {(row["owner"],row["name"]) for row in records} == required
    if phase == "gap":
        observations = re.findall(r"GAP_ACK mode=(default|enabled) gap_bytes=2 duplicate_ack=([0-9]+) cumulative_ack=([0-9]+) at_ms=(0|40)\b", logtext)
        r["packets"] = {name: [int(dup), int(ack), int(ms)] for name, dup, ack, ms in observations}
        save("result.json", r)
        assert len(observations) == 2 and r["packets"] == {"default":[1290,1292,0], "enabled":[1290,1292,0]}
        r["finding"] = "GAP_ACK_IMMEDIATE"
    else:
        observations = re.findall(r"restored: ACK (\d+) by 40ms = (true|false), ACK (\d+) after new data", logtext)
        (owed, by_deadline, advanced), = observations
        r["packets"] = {"pre_checkpoint_unread":500, "ack":int(owed), "by_40ms":by_deadline=="true", "ack_after_new_bytes":int(advanced)}
        save("result.json", r)
        assert (owed, advanced) == ("1790", "2790")
        r["finding"] = "ACK_BY_DEADLINE" if by_deadline=="true" else "ACK_MISSING_AT_DEADLINE"
    if phase == "stock" and r["finding"] == "ACK_MISSING_AT_DEADLINE":
        assert code == 3 and v["status"] == "FAIL" and not m["errors"]
        assert not v.get("errors") and not v.get("missing") and not v.get("skips")
        assert r["executions"][0]["status"] == "FAILED"
        assert {row["name"]:row["result"] for row in records} == {"TestDelayedACKTimeout":"pass", restore_case:"failure"}
        assert "restored TCP receiver lost its pending ACK" in logtext
        r["disposition"] = "EXPECTED_TEST_FAILURE"
    else:
        assert code == 0 and v["status"] == "PASS" and not any(r["report_problem_counts"].values())
        assert all(run["status"] == "PASSED" for run in r["executions"]) and all(row["result"] == "pass" for row in records)
        r["disposition"] = "ALL_CASES_PASSED"
    r["status"] = "PASS"
except (AssertionError, KeyError, ValueError, OSError, RuntimeError, subprocess.SubprocessError, ET.ParseError) as exc:
    r["collector_error"] = type(exc).__name__
    r["collector_line"] = traceback.extract_tb(exc.__traceback__)[-1].lineno
finally:
    r["verified_at"] = now()
    save("result.json", r)
sys.exit(int(r["status"] != "PASS"))
