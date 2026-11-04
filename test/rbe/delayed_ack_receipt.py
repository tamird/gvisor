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

source, destination = sys.argv[1:]
owner = "//pkg/tcpip/transport/tcp/test/e2e:delayed_ack_qualification_test"
wanted = "TestDelayedACKGapQualification"
control = "TestDelayedACKTimeout"
owners = [owner]
required = {(owner, n) for n in (control, wanted, wanted+"/default", wanted+"/enabled")}
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
    assert r["runner_os"] == "24.04" and os.uname().machine == "x86_64"
    stat = os.statvfs(d)
    r["disk_free_bytes_at_start"] = stat.f_bavail * stat.f_frsize
    r["memory_bytes"] = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    r["logical_cpus"] = len(os.sched_getaffinity(0))
    r["started_at"] = now()
    save("result.json", r)
    assert r["disk_free_bytes_at_start"] >= 12 * 1024**3 and r["memory_bytes"] >= 6 * 1024**3 and r["logical_cpus"] >= 2
    assert git("rev-parse", "HEAD").decode().strip() == source
    assert source == "b41d5a16e248cf2cc1f5055f5d3711a1a926e6de" and git("rev-parse", "HEAD^").decode().strip() == "6dcadf4a84a07f69e8eb5f1ea9d7b2903bd03bd7"
    r.update(submitted="97785277ac1befb39123fbf5a8706793e5dc8eab", export_parent="e0105ccf200aa879bb21c212a6b8e6d91ff97378", master="6dcadf4a84a07f69e8eb5f1ea9d7b2903bd03bd7")
    r["submitted_blobs"] = {'pkg/tcpip/tcpip.go': ('e46697f2fb0e4bc6dd36f4afad36124a5c113a1b', 'f59123c3df3c42e55f75befeffaa8ec3c5b75c2a'), 'pkg/tcpip/transport/tcp/connect.go': ('2d34a5a4294269fb9a3b1e9df7369ac59cfc87df', '08eff25e75b1dc4ac55da913e4f8387699156b71'), 'pkg/tcpip/transport/tcp/endpoint.go': ('1af9ce9795c68e5cfd9a3408cb37dfed8fe8a8d7', '9cd44f46b0aeacd18951fb93ff1cb55399e42f8e'), 'pkg/tcpip/transport/tcp/endpoint_state.go': ('f4102ec7955fb45e01aff8aca8b19df6307c21be', 'ae05eb5f062fe365dac62407c2f1af7c755893c6'), 'pkg/tcpip/transport/tcp/protocol.go': ('aa4f2d15f217dd1161a3377acdb217a608a7f3a7', 'c144e243de590a8762557336b8a65273afddccb9'), 'pkg/tcpip/transport/tcp/snd.go': ('fc6be6693435f1518f6e06945e8c318f30436487', '6d74a5addb5c44898d1312e534a1b4c901235fcc'), 'pkg/tcpip/transport/tcp/test/e2e/tcp_delayed_ack_test.go': (None, '78b373d6c45956e62f775c45edca5faa88013e2d')}
    for path, (before, after) in r["submitted_blobs"].items():
        assert git("rev-parse", "HEAD:"+path).decode().strip() == after
        if before is not None:
            assert git("rev-parse", "HEAD^:"+path).decode().strip() == before
    r["tree"] = git("rev-parse", "HEAD^{tree}").decode().strip()
    (d/"before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    assert not (d/"before").read_bytes()
    (d/"commit").write_bytes(git("cat-file", "commit", source))
    bazel = ["bazel", "--nosystem_rc", "--nohome_rc", "--bazelrc=/dev/null", "--host_jvm_args=-Xmx3g"]
    options = []
    args = ["test", *options, "--compilation_mode=opt", "--jobs=4", "--local_resources=memory=8192",
            "--remote_cache=", "--remote_executor=", "--bes_backend=", "--spawn_strategy=local", "--strategy=TestRunner=local",
            "--test_filter=^(" + wanted + "|" + control + ")$", "--test_timeout=120", "--test_arg=-test.v", "--nocache_test_results", "--test_sharding_strategy=disabled", "--runs_per_test=1", "--flaky_test_attempts=1",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--build_event_json_file=" + str(d/"events.raw"),
            "--build_event_json_file_path_conversion=false",
            *owners]
    r["arguments"] = [arg for arg in args if not arg.startswith("--build_event_json_file=")]
    r["timeout_seconds"] = 3300
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
            records.append({"name":case.get("name") if (owner,case.get("name")) in required else "<unexpected>","owner":f["identity"]["label"],
                            "result":"skipped" if case.get("status")=="notrun" or case.get("result")=="suppressed" else next((c.tag for c in case if c.tag in ("skipped","failure","error")),"pass")})
    r["cases"]=records
    assert code == 0 and v["status"] == "PASS" and not any(r["report_problem_counts"].values())
    run, = r["executions"]
    assert run["owner"] == owner and run["strategy"] == "local" and not run["cached"] and run["status"] == "PASSED"
    assert len(records) == 4 and {(row["owner"], row["name"]) for row in records} == required and all(row["result"] == "pass" for row in records)
    assert {f["name"] for f in r["reports"]} == {"test.xml", "test.log"} and len(r["reports"]) == 2
    assert all(f["identity"] == run["identity"] for f in r["reports"])
    logpath, = (d/f["path"] for f in m["reports"] if f["name"] == "test.log")
    observations = re.findall(r"GAP_ACK mode=(default|enabled) gap_bytes=2 duplicate_ack=([0-9]+) cumulative_ack=([0-9]+) at_ms=(0|40)\b", logpath.read_text(errors="replace"))
    assert len(observations) == 2 and {name for name, *_ in observations} == {"default", "enabled"}
    r["packets"] = {name: {"gap_bytes":2, "duplicate_ack":int(dup), "cumulative_ack":int(ack), "observed_at_ms":int(ms)} for name, dup, ack, ms in observations}
    default, enabled = r["packets"]["default"], r["packets"]["enabled"]
    assert default["observed_at_ms"] == 0 and default["duplicate_ack"]+2 == default["cumulative_ack"]
    assert all(default[k] == enabled[k] for k in ("duplicate_ack", "cumulative_ack"))
    r["finding"] = "GAP_ACK_DELAYED" if enabled["observed_at_ms"] == 40 else "GAP_ACK_IMMEDIATE"
    r["status"] = "PASS"
except (AssertionError, KeyError, ValueError, OSError, RuntimeError, subprocess.SubprocessError, ET.ParseError) as exc:
    r["collector_error"] = type(exc).__name__
    r["collector_line"] = traceback.extract_tb(exc.__traceback__)[-1].lineno
finally:
    r["verified_at"] = now()
    save("result.json", r)
sys.exit(int(r["status"] != "PASS"))
