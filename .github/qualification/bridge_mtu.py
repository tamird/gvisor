"""Compare the existing bridge syscall regression on Linux and two Sentries."""

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
import zipfile

import qualification_report as report

ROOT = Path(os.environ["RUNNER_TEMP"])
PUBLIC = ROOT / "bridge-public"
PRODUCT = "735639f573bb921490150d0c722414c124119728"
STOCK = "03e75796c4fe287faa7a5e0f6a85ae81c044f896"
BASE = "b21a9d4eea7b0066232c33c63df9cf20ed987f6f"
CASE = "NetlinkRouteTest/BridgeAddUnnamed"
SECRETS = [value for key in ("GH_TOKEN", "GITHUB_TOKEN", "BUILDBUDDY_API_KEY")
           if (value := os.environ.get(key))]


def utc():
    return datetime.now(timezone.utc).isoformat()


def git(*args):
    return subprocess.check_output(["git", *args]).decode().strip()


def save():
    result["verdict_available_at"] = utc()
    data = json.dumps(result, indent=2, sort_keys=True) + "\n"
    assert len(data.encode()) < 128 * 1024
    assert not any(value in data or json.dumps(value)[1:-1] in data for value in SECRETS)
    assert not re.search(r"(?i)(?:authorization|x-buildbuddy-api-key)\s*[:=]|github_pat_|gh[pousr]_", data)
    (PUBLIC / "verdict.json").write_text(data)


def original_only():
    """Read bounded original console records; never repeat a product command."""
    from cas_inputs import fetch

    data = (ROOT / "original.zip").read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    assert digest == "c22c2c2abc04889d426e9e8190ac528e4f2f0a78eb14bc0ad82f4137f0814323"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ["verdict.json"]
        original = json.loads(archive.read("verdict.json"))
    assert original["source"] == "4b64fa6de52a1b145ed86bb39585ca918d9a1153"
    assert original["product"] == PRODUCT and original["stock"] == STOCK
    assert original["status"] == "INCOMPLETE" and set(original["arms"]) == {"native"}
    native = original["arms"]["native"]
    assert native["native_exit"] == 124 and native["reports"] == []
    assert native["target"] == "//test/syscalls:socket_netlink_route_test_native"
    identifier = "bbcaf9bd-0664-43b0-9ef9-87344330eeab"
    assert native["invocation"] == identifier and native["source"] == STOCK
    result.update(original=original, original_zip_sha256=digest, workload_repeated=False, console={})
    save()
    public_source = git("show", "HEAD:MODULE.bazel") + git(
        "show", "4b64fa6de52a1b145ed86bb39585ca918d9a1153:.github/workflows/build.yml")
    public_urls = [urlsplit(value) for value in re.findall(r"https?://[^\s\]\"']+", public_source)]
    public_hosts = {url.hostname for url in public_urls}
    public_archives = {(url.hostname, Path(url.path).name) for url in public_urls}
    patterns = {
        "download": r"Error downloading",
        "timeout": r"timed out|TimeoutException|DEADLINE_EXCEEDED",
        "dns": r"UnknownHostException", "tls": r"SSLHandshakeException|PKIX",
        "checksum": r"[Cc]hecksum.*(?:match|expected)",
        "missing_package": r"no such package|no such target",
        "analysis_failure": r"Analysis of target.*failed|analysis failed",
        "compiler_failure": r"error:|fatal error:|undeclared inclusion|error executing",
        "remote_error": r"UNAVAILABLE|Failed to execute|Error executing",
        "interrupted": r"interrupted|interrupt|SIGINT",
        "memory": r"Out of memory|out of memory|Cannot allocate memory",
    }
    progress = ("Loading", "Analyzing", "Downloading", "Compiling", "Linking", "GoCompilePkg", "GoLink", "remote", "local", "Running", "Testing")
    for kind, token in (("tail", None), ("forward", "0")):
        chunks = []
        complete = False
        row = result["console"][kind] = dict(complete=False, pages=0)
        try:
            for _ in range(1 if kind == "tail" else 32):
                payload = {"selector": {"invocation_id": identifier}}
                if token is not None:
                    payload["page_token"] = token
                page = json.loads(fetch("api/v1/GetLog", payload))
                chunk = page.get("log", {}).get("contents", "").encode()
                assert sum(map(len, chunks)) + len(chunk) <= 4 * 1024 * 1024
                chunks.append(chunk)
                token = page.get("next_page_token", page.get("nextPageToken"))
                row["pages"] = len(chunks)
                if not token:
                    complete = True
                    break
        except Exception as error:
            row["lookup_error"] = type(error).__name__
        raw = b"".join(chunks)
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw.decode(errors="replace"))
        diagnostics, stages = [], []
        for line in text.splitlines():
            if len(line) > 8192 or re.search(r"(?i)authorization|x-buildbuddy-api-key|command line|--remote_header|--bes_header", line):
                continue
            classes = [name for name, pattern in patterns.items() if re.search(pattern, line)]
            if classes and len(diagnostics) < 24:
                urls = []
                for value in re.findall(r"https?://[^\s\]\"']+", line):
                    try:
                        url = urlsplit(value)
                    except ValueError:
                        continue
                    basename = Path(url.path).name
                    if url.hostname in public_hosts and url.hostname and re.fullmatch(r"[A-Za-z0-9.-]{1,160}", url.hostname):
                        filename = basename if (url.hostname, basename) in public_archives and re.fullmatch(r"[A-Za-z0-9._+-]{1,110}\.(?:tar(?:\.(?:gz|xz|bz2|zst))?|tgz|zip|whl|mod|sum)", basename) else None
                        urls.append(dict(host=url.hostname, filename=filename))
                diagnostics.append(dict(classes=classes, line_sha256=hashlib.sha256(line.encode()).hexdigest(), urls=urls[:4],
                    http_codes=re.findall(r"(?i)(?:HTTP(?:/[0-9.]+)?\s+|(?:status(?:\s+code)?|response code)\s*[:=]?\s*)([45][0-9]{2})\b", line)[:4]))
            counts = re.search(r"\[\s*([0-9,]+)\s*/\s*([0-9,]+)\s*\]", line)
            stages.append(dict(classes=[word for word in progress if re.search(r"\b" + word + r"\b", line)],
                               actions=[int(counts[i].replace(",", "")) for i in (1, 2)] if counts else []))
        row.update(complete=complete, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                   diagnostics=diagnostics, latest_progress=[stage for stage in stages if stage["classes"] or stage["actions"]][-12:])
        save()
    result["status"] = "ORIGINAL_LOGS_READ" if all(row.get("bytes", 0) and not row.get("lookup_error") for row in result["console"].values()) else "INCOMPLETE"
    result["console_forward_complete"] = result["console"]["forward"]["complete"]
    save()


def execute(name, source, target):
    git("checkout", "--quiet", "--detach", source)
    out = ROOT / "bridge-private" / name
    out.mkdir(parents=True)
    for field, args in {
        "commit_file": ("cat-file", "commit", "HEAD"),
        "source_before": ("status", "--porcelain", "--untracked-files=no"),
    }.items():
        (out / field).write_bytes(subprocess.check_output(["git", *args]))
    raw = out / "raw.jsonl"
    args = ["test", "--config=x86_64", "--keep_going", "--strip=never",
            "--lockfile_mode=off", "--test_filter=NetlinkRouteTest.BridgeAddUnnamed", "--test_sharding_strategy=disabled",
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_timeout=180",
            "--remote_download_outputs=toplevel", "--test_output=errors",
            f"--build_event_json_file={raw}", "--build_event_json_file_path_conversion=false", target]
    (out / "executed_arguments").write_text("\n".join(args) + "\n")
    command = ["docker", "exec", "bridge-gnu", "timeout", "--signal=INT", "--kill-after=30s", "1080",
               "bazel", f"--bazelrc={ROOT / 'bridge.bazelrc'}", *args]
    with (out / "console").open("wb") as stream:
        code = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT).returncode
    finished = utc()
    row = dict(source=source, target=target, native_exit=code, command_finished_at=finished,
               status="FAIL" if code == 3 else "INCOMPLETE")
    result["arms"][name] = row
    save()
    (out / "native_exit").write_text(str(code))
    (out / "source_after").write_text(git("status", "--porcelain", "--untracked-files=no"))
    (out / "final_head").write_text(git("rev-parse", "HEAD"))
    (out / "targets").write_text(target + "\n")
    events = out / "events.jsonl"
    captured = report.capture(raw, events, Path("bazel-out"), out / "reports")
    (out / "capture_exit").write_text("1" if captured["errors"] else "0")
    context = {key: key for key in ("commit_file", "source_before", "source_after", "final_head",
                                   "executed_arguments", "native_exit", "capture_exit")}
    context.update(expected_source=source, command_kind="test", execution_finished_at=finished)
    report.write_json(out / "context.json", context)
    verdict = report.verdict(events, report.manifest_path(events), out / "context.json", out / "targets", ())
    row.update(original_status=verdict["status"], case_counts=verdict["case_counts"],
               source_clean=not verdict["errors"], evidence_complete=not (verdict["errors"] or verdict["missing"]),
               reporting_elapsed_seconds=verdict["reporting_elapsed_seconds"],
               execution_to_verdict_seconds=verdict["execution_to_verdict_seconds"])
    records = [json.loads(line) for line in events.read_text().splitlines()]
    starts = [item["started"]["uuid"] for item in records if "started" in item]
    assert len(starts) == 1 and re.fullmatch(r"[a-f0-9-]{36}", starts[0])
    row["invocation"] = starts[0]
    row["reports"] = [{key: item[key] for key in ("name", "sha256", "bytes")} for item in captured["reports"]]
    attempts = [item for item in records if "testResult" in item]
    assert len(attempts) == 1 and len(verdict["executed"]) == 1
    attempt = attempts[0]
    identity, observation = attempt["id"]["testResult"], attempt["testResult"]
    assert identity["label"] == target and re.fullmatch(r"[a-f0-9]{64}", identity["configuration"]["id"])
    assert all(identity.get(key, 1) == 1 for key in ("run", "shard", "attempt"))
    info = observation["executionInfo"]
    row["attempt"] = dict(configuration=identity["configuration"]["id"],
                          strategy=info.get("strategy"), cached_locally=observation.get("cachedLocally", False),
                          cached_remotely=info.get("cachedRemotely", False), status=observation["status"])
    row["test_timing"] = {key: observation[key] for key in (
        "testAttemptStart", "testAttemptStartMillis", "testAttemptDuration", "testAttemptDurationMillis") if key in observation}
    save()
    assert row["evidence_complete"] and info.get("strategy") == "remote"
    assert not observation.get("cachedLocally") and not info.get("cachedRemotely")
    xml, = [out / item["path"] for item in captured["reports"] if item["name"] == "test.xml"]
    case, = ET.parse(xml).getroot().iter("testcase")
    assert "/".join((case.get("classname", ""), case.get("name", ""))) == CASE
    assert case.find("skipped") is None and case.get("status") != "notrun" and case.get("result") != "suppressed"
    if name == "stock":
        assert observation["status"] == "FAILED"
        failures = case.findall("failure")
        texts = ["".join(item.itertext()) for item in failures]
        patterns = (r"bridge\.mtu\s+Which is: 1486\s+1500", r"updated.mtu\s+Which is: 1446\s+1460")
        assert len(texts) == 2 and all(any(re.search(pattern, text) for text in texts) for pattern in patterns)
        assert code == 3 and verdict["status"] == "FAIL" and row["case_counts"] == dict(passed=0, failed=1, skipped=0)
        row.update(status="EXPECTED_FAILURE", observed_mtu=[1486, 1446], requested_mtu=[1500, 1460])
    else:
        assert code == 0 and verdict["status"] == "PASS" and row["case_counts"] == dict(passed=1, failed=0, skipped=0)
        row["status"] = "PASS"
    save()


result = dict(source=os.environ["GITHUB_SHA"], product=PRODUCT, stock=STOCK, base=BASE,
              started_at=utc(), status="INCOMPLETE", arms={})
save()
if sys.argv[1:] == ["--original-only"]:
    original_only()
    sys.exit(result["status"] != "ORIGINAL_LOGS_READ")
try:
    assert git("rev-parse", "HEAD^") == STOCK
    assert git("rev-parse", STOCK + "^") == PRODUCT
    assert git("rev-parse", PRODUCT + "^") == BASE
    assert git("diff", "--name-only", STOCK, PRODUCT) == "pkg/tcpip/stack/bridge.go"
    assert git("rev-parse", STOCK + ":pkg/tcpip/stack/bridge.go") == git("rev-parse", BASE + ":pkg/tcpip/stack/bridge.go")
    assert git("diff", "--name-only", BASE, PRODUCT).splitlines() == [
        "pkg/tcpip/stack/bridge.go", "pkg/tcpip/stack/bridge_test.go", "test/syscalls/linux/socket_netlink_route.cc",
        "test/syscalls/linux/socket_netlink_route_util.cc", "test/syscalls/linux/socket_netlink_route_util.h",
        "test/syscalls/linux/tuntap.cc"]
    for name, source, suffix in (("native", STOCK, "native"), ("stock", STOCK, "runsc_systrap_shared"),
                                  ("fixed", PRODUCT, "runsc_systrap_shared")):
        try:
            execute(name, source, "//test/syscalls:socket_netlink_route_test_" + suffix)
        except Exception as error:
            result["arms"].setdefault(name, {"status": "INCOMPLETE"})["verifier_error"] = type(error).__name__
            save()
            if name == "native":
                break
    statuses = {name: row["status"] for name, row in result["arms"].items()}
    result["status"] = "PASS" if statuses == {"native": "PASS", "stock": "EXPECTED_FAILURE", "fixed": "PASS"} else (
        "FAIL" if "FAIL" in statuses.values() else "INCOMPLETE")
finally:
    save()
assert result["status"] == "PASS"
