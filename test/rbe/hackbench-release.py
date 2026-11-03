"""Replay the established two-architecture Hackbench image/Go consumer proof."""

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tarfile
import time
from urllib.parse import urlsplit, urlunsplit
import xml.etree.ElementTree as ET
import zipfile

PRODUCT = "7c63124a29588f4cdc264e11b8f345230c6efed3"
COPYING = "ab15fd526bd8dd18a9e77ebc139656bf4d33e97fc7238cd11bf60e2b9b8666c6"
TMP = Path(os.environ["RUNNER_TEMP"])
PRIVATE = TMP / "hackbench-private"
PUBLIC = TMP / "hackbench-public"
PRIVATE.mkdir(exist_ok=True)
PUBLIC.mkdir(exist_ok=True)
RESULT = {"product": PRODUCT, "carrier": os.environ["GITHUB_SHA"], "phases": {}}
BZ = ["bazel", "--bazelrc=" + str(TMP / "images.bazelrc")]
STARTED = time.monotonic()
TARGET = "//test/benchmarks/base:hackbench_test"
TEST_FLAGS = ["--jobs=4", r"--strategy_regexp=Compiling src/google/protobuf/.*\.cc=linux-sandbox",
              "--strategy=TestRunner=local", "--nocache_test_results", "--test_output=errors",
              "--test_arg=-test.v", "--test_arg=-test.run=^$", "--test_arg=-test.bench=^BenchmarkHackbench$",
              "--test_arg=-test.benchtime=1x", "--test_arg=-runtime=runc", "--test_timeout=600"]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save() -> None:
    data = json.dumps(RESULT, indent=2).encode() + b"\n"
    for key, value in os.environ.items():
        if ("TOKEN" in key or "SECRET" in key or "API_KEY" in key) and len(value) >= 12:
            assert value.encode() not in data, f"Secret in report: {key}"
    rc = TMP / "images.bazelrc"
    if rc.exists():
        for value in re.findall(r"x-buildbuddy-api-key=([^\s]+)", rc.read_text()):
            assert value.encode() not in data, "Remote credential in report"
    assert b"x-buildbuddy-api-key" not in data and b"Bearer " not in data
    (PUBLIC / "result.json").write_bytes(data)


def summarize_bep(path: Path) -> dict:
    """Project the current complete records; a concurrently written tail may be partial."""
    events = []
    data = path.read_bytes() if path.exists() else b""
    if data:
        for line in data.splitlines():
            try:
                events.append(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                break
    return {"bytes": len(data),
            "sha256": sha(data) if data else None,
            "uuid": next((e["started"].get("uuid") for e in events if "started" in e), None),
            "completions": [{"id": e["id"]["targetCompleted"], "success": e.get("completed", {}).get("success")}
                            for e in events if "targetCompleted" in e.get("id", {}) and e["id"]["targetCompleted"]["label"] == TARGET],
            "attempts": [{"id": e["id"]["testResult"], "status": e.get("testResult", {}).get("status")}
                         for e in events if "testResult" in e.get("id", {}) and e["id"]["testResult"]["label"] == TARGET]}


def run(name: str, args: list[str], row: dict, timeout: int = 600) -> subprocess.CompletedProcess:
    timeout = min(timeout, int(75 * 60 - (time.monotonic() - STARTED)))
    assert timeout > 0, "Recovery command budget exhausted"
    log = PRIVATE / (name + ".log")
    item = {"command": args, "started": now(), "exit": None, "timeout_seconds": timeout}
    row.setdefault("commands", []).append(item)
    save()
    started = time.monotonic()
    bep = next((Path(arg.split("=", 1)[1]) for arg in args if arg.startswith("--build_event_json_file=")), None)

    def snapshot() -> None:
        item["elapsed_seconds"] = round(time.monotonic() - started, 3)
        lines = log.read_text(errors="replace").splitlines()
        # Public diagnostics exclude command/environment dumps and all URLs.
        item["diagnostics"] = [line[:900] for line in lines if re.match(
            r"^(ERROR:|WARNING:|FAIL:|INFO: (Analyzed|Found|Elapsed|Build)|\[[0-9, /]+\] )", line)
            and not re.search(r"(?i)authorization|token|api-key|https?://|remote_header|bes_header", line)][-30:]
        if bep is not None:
            try:
                item["bep"] = summarize_bep(bep)
            except Exception as error:
                item["bep_error"] = type(error).__name__
        save()

    with log.open("wb") as output_file:
        process = subprocess.Popen(args, stdout=output_file, stderr=subprocess.STDOUT, start_new_session=True)
        while process.poll() is None:
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                pass
            snapshot()
            if process.poll() is None and time.monotonic() - started >= timeout:
                item["timed_out"] = True
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    snapshot()
    data = log.read_bytes()
    item.update(finished=now(), exit=124 if item.get("timed_out") else process.returncode,
                log_sha256=sha(data), log_bytes=len(data))
    save()
    return subprocess.CompletedProcess(args, item["exit"], data.decode(errors="replace"))


def build_consumer(row: dict) -> None:
    bep = PRIVATE / "consumer-build.bep.json"
    result = run("consumer-build", BZ + ["build", *TEST_FLAGS, "--show_progress", "--color=no", "--curses=no",
                 "--build_event_json_file=" + str(bep), TARGET], row, timeout=2700)
    assert result.returncode == 0, "Consumer build did not complete"


def build_proof(row: dict) -> None:
    row.update(summarize_bep(PRIVATE / "consumer-build.bep.json"))
    assert re.fullmatch(r"[a-f0-9-]{36}", row["uuid"]), "Missing original build UUID"
    assert any(e["success"] and not e["id"].get("aspect") for e in row["completions"]), "No successful owner completion"


def consumer_binary(row: dict) -> None:
    binary = output("consumer-path", TARGET, [], row)
    data = binary.read_bytes()
    assert data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[18:20], "little") == 62
    row["binary"] = {"path": str(binary), "size": len(data), "sha256": sha(data)}
    # Preserve the executable itself; its native Bazel runfiles remain in this job.
    for value in re.findall(r"x-buildbuddy-api-key=([^\s]+)", (TMP / "images.bazelrc").read_text()):
        assert value.encode() not in data
    for key, value in os.environ.items():
        if ("TOKEN" in key or "SECRET" in key or "API_KEY" in key) and len(value) >= 12:
            assert value.encode() not in data, f"Secret in executable: {key}"
    (PUBLIC / "hackbench_test").write_bytes(data)
    save()


def checked(name: str, args: list[str], row: dict) -> str:
    p = run(name, args, row)
    assert p.returncode == 0, f"{name} exited {p.returncode}"
    return p.stdout


def output(name: str, target: str, flags: list[str], row: dict) -> Path:
    lines = checked(name, BZ + ["cquery", *flags, "--output=files", target], row).splitlines()
    files = [Path(line) for line in lines if line.startswith("bazel-out/")]
    assert len(files) == 1 and files[0].is_file(), files
    return files[0]


def image(arch: str, cpu: str, machine: int, row: dict) -> None:
    flags = ["--config=" + arch]
    target = "//images/benchmarks/hackbench:hackbench_context"
    checked(arch + "-build", BZ + ["build", "--experimental_remote_require_cached", *flags, target], row)
    archive = output(arch + "-path", target, flags, row)
    row["context"] = {"size": archive.stat().st_size, "sha256": sha(archive.read_bytes())}
    assert row["context"] == RESULT["original"]["phases"][cpu]["context"]
    with tarfile.open(archive) as tar:
        members = [m for m in tar.getmembers() if m.name.removeprefix("./") ==
                   "images/benchmarks/hackbench/image.tar"]
        assert len(members) == 1 and members[0].isfile(), members
        f = tar.extractfile(members[0])
        assert f is not None
        saved = PRIVATE / (arch + "-image.tar")
        saved.write_bytes(f.read())
    checked(arch + "-load", ["docker", "load", "-i", str(saved)], row)
    tag = "gvisor.dev/images/benchmarks/hackbench:latest"
    info = json.loads(checked(arch + "-inspect", ["docker", "image", "inspect", tag], row))[0]
    assert info["Architecture"] == cpu and info["Os"] == "linux", info["Architecture"]
    row["image"] = {"id": info["Id"], "architecture": cpu, "os": info["Os"],
                    "layers": info["RootFS"]["Layers"]}
    assert row["image"] == RESULT["original"]["phases"][cpu]["image"]
    target = "//test/benchmarks/base:hackbench_test"
    bep = PRIVATE / (arch + "-benchmark.bep.json")
    args = BZ + ["test", *TEST_FLAGS, "--experimental_remote_require_cached",
                 "--build_event_json_file=" + str(bep), target]
    p = run(arch + "-benchmark", args, row, timeout=900)
    base = Path("bazel-testlogs/test/benchmarks/base/hackbench_test")
    log = (base / "test.log").read_text() if (base / "test.log").exists() else ""
    row["benchmark_exit"] = p.returncode
    assert p.returncode == 0, "Real benchmark failed"
    events = [json.loads(line) for line in bep.read_text().splitlines()]
    attempts = [e for e in events if "testResult" in e.get("id", {})]
    assert len(attempts) == 1 and attempts[0]["id"]["testResult"]["label"] == target
    result = attempts[0]["testResult"]
    assert result["status"] == "PASSED" and not result.get("cachedLocally", False)
    assert result.get("executionInfo", {}).get("strategy") == "local", result.get("executionInfo")
    row["attempt"] = {"id": attempts[0]["id"]["testResult"], "status": result["status"],
                      "execution": result.get("executionInfo"), "cachedLocally": False}
    xml = (base / "test.xml").read_bytes()
    root = ET.fromstring(xml)
    assert not root.findall(".//failure") and not root.findall(".//error") and not root.findall(".//skipped")
    assert "--- FAIL:" not in log and "--- SKIP:" not in log
    rows = []
    for case in ("BenchmarkHackbench/ipcMode.pipe/processMode.thread", "BenchmarkHackbench/ipcMode.socket/processMode.process"):
        found = [s for s in log.splitlines() if s.startswith(case) and "execution_time_per_loop" in s]
        assert len(found) == 1, (case, found)
        rows.extend(found)
    row["benchmark_rows"] = rows
    row["xml_sha256"] = sha(xml)
    row["log_sha256"] = sha(log.encode())
    row["runtime"] = "native AMD64 runc" if cpu == "amd64" else "QEMU ARM64 under AMD64 runc"


def original(row: dict) -> None:
    data = (TMP / "original.zip").read_bytes()
    expected = "ac28f02d7d1b4feb0752487b29e061c99646a41db2607ade29e33ed93beb2e9c"
    assert sha(data) == expected
    with zipfile.ZipFile(TMP / "original.zip") as z:
        assert z.namelist() == ["result.json"]
        old = json.loads(z.read("result.json"))
    assert old["product"] == PRODUCT and old["carrier"] == "dd90552017925a5659d77e0feb1056af11fc86a7"
    assert old["status"] == "FAIL" and set(old["phases"]) == {"amd64", "arm64", "catalog"}
    assert old["phases"]["amd64"]["error"] == "AssertionError: x86_64-help exited 1"
    assert old["phases"]["arm64"]["error"] == "AssertionError: aarch64-elf-copy exited 1"
    catalog = old["phases"]["catalog"]
    assert catalog["status"] == "PASS" and catalog["commands"][0]["exit"] == 0
    assert catalog["verification"] == "all 221 dependencies have up-to-date license entries that conform to governance/licensing.yaml"
    RESULT["original"] = old
    row["artifact"] = "https://github.com/tamird/gvisor/actions/runs/41520993482/artifacts/13178695451"
    row["archive_sha256"] = expected
    row["catalog_reused"] = True
    timeout_zip = TMP / "timeout-original.zip"
    assert sha(timeout_zip.read_bytes()) == "97490a76a86b3ee57dab8dac43c998fbe8a51207b92c77dc5b4d20203ba08a71"
    with zipfile.ZipFile(timeout_zip) as archive:
        receipt = json.loads(archive.read("result.json"))
    assert receipt["product"] == PRODUCT and receipt["carrier"] == "e999b2a936ca18bcd69af550c3697759b34f5d8c"
    RESULT["prior_timeout"] = {"run": 41522986904, "status": "cancelled: exceeded maximum execution time",
        "receipt_sha256": sha(timeout_zip.read_bytes()), "record": receipt["phases"]["terminal_diagnosis"]}


def phase(name: str, fn: Callable[[dict], None]) -> None:
    row = {"started": now(), "status": "INCOMPLETE"}
    RESULT["phases"][name] = row
    try:
        fn(row)
        row["status"] = "PASS"
    except Exception as e:
        row["status"] = "FAIL"
        row["error"] = f"{type(e).__name__}: {e}"[:1200]
    finally:
        row["finished"] = now()
        save()


def terminal_diagnosis(row: dict) -> None:
    """Recover the original timeout's causal context without executing a workload."""
    from cas_inputs import fetch, rpc

    data = (TMP / "diagnosis-original.zip").read_bytes()
    assert sha(data) == "07a0e2de2a429bb6f77b4f3436690eb6c7710dc46a5b1321d99f93047894b3f3"
    with zipfile.ZipFile(TMP / "diagnosis-original.zip") as z:
        prior = json.loads(z.read("result.json"))["phases"]["hackbench_diagnosis"]
    matches = [entry for entry in prior["invocations"]
               if entry["invocationId"] == "17603d7e-5f53-4911-8148-b5cf0976495c"]
    old, = matches
    assert old["commitSha"] == "41b305bcc71d303421044555b4f1ddeccbcaed5a"
    assert old["command"] == "test" and old["pattern"] == ["//test/benchmarks/base:hackbench_test"]
    assert old["invocationStatus"] == "DISCONNECTED_INVOCATION_STATUS"
    row.update(original_archive_sha256=sha(data), original_run=41522986904,
               original=old, new_workloads=0)
    save()
    token = ""
    chunks = []
    for _ in range(4):
        response = json.loads(fetch("api/v1/GetLog", {"selector": {"invocation_id": old["invocationId"]}, "page_token": token}))
        chunks.append(response.get("log", {}).get("contents", ""))
        token = response.get("next_page_token", response.get("nextPageToken", ""))
        if not token:
            break
    text = "".join(chunks)
    row["log_sha256"] = sha(text.encode())
    row["partial"] = bool(token)
    assert not token and row["log_sha256"] == old["log_projection"]["sha256"], "Original console bytes changed"
    normalized = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    lines = normalized.splitlines()
    row["console"] = {"bytes": len(text.encode()), "lines": len(lines), "ansi_removed": normalized != text}
    # Keep diagnostic context, not environment, complete command lines or logs.
    safe_lines = [re.sub(r'https?://[^\s\"<>]+', lambda m: urlunsplit((urlsplit(m[0]).scheme, urlsplit(m[0]).hostname or "", urlsplit(m[0]).path, "", "")), line) for line in lines]
    row["diagnostics"] = [line[:900] for line in safe_lines
        if line.startswith(("ERROR:", "WARNING:", "FAIL:", "INFO: Analyzed", "INFO: Invocation", "INFO: Elapsed",
                            "INFO: Build", "INFO: Found", "INFO: Waiting", "INFO: Streaming", "DEBUG:", "Executed "))
        and not re.search(r"(?i)authorization|bearer|remote_header|bes_header|x-buildbuddy|github_pat_|gh[pousr]_[A-Za-z0-9]{20}", line)][:80]
    row["progress"] = [line[:900] for line in safe_lines
        if (line.startswith(("Loading:", "Analyzing:", "Computing main repo mapping:", "Fetching "))
            or re.match(r"^\[[0-9, /]+\] ", line))
        and not re.search(r"(?i)authorization|bearer|remote_header|bes_header|x-buildbuddy|github_pat_|gh[pousr]_[A-Za-z0-9]{20}", line)][-40:]
    save()
    row["structured_status"] = "INCOMPLETE"
    try:
        response = rpc("GetInvocation", {"lookup": {"invocationId": old["invocationId"]}})
        inv, = response["invocation"]
        assert inv["invocationId"] == old["invocationId"] and inv["commitSha"] == old["commitSha"]
        row["latest_invocation"] = {key: inv.get(key) for key in ("invocationId", "commitSha", "command", "pattern",
                                  "invocationStatus", "success", "bazelExitCode", "durationUsec", "actionCount", "targetConfiguredCount")}
        # Only event kinds and the selected target's public fields leave this job.
        events = [entry.get("buildEvent", {}) for entry in inv.get("event", [])]
        kinds = {}
        for event in events:
            for key in event.get("id", {}):
                kinds[key] = kinds.get(key, 0) + 1
        row["event_kinds"] = kinds
        # GetInvocation excludes target/progress events. Query the named owner
        # explicitly; an absent retained event does not prove it never ran.
        target = rpc("GetTarget", {"invocationId": old["invocationId"],
                                  "targetLabel": "//test/benchmarks/base:hackbench_test"})
        groups = target.get("targetGroups", [])
        assert not any(group.get("nextPageToken") for group in groups), "Named owner metadata is partial"
        row["targets"] = []
        for group in groups:
            for item in group.get("targets", []):
                assert item["metadata"]["label"] == "//test/benchmarks/base:hackbench_test"
                row["targets"].append({"label": item["metadata"]["label"], "status": item.get("status"),
                    "rootCause": item.get("rootCause"), "test_result_events": len(item.get("testResultEvents", [])),
                    "action_events": len(item.get("actionEvents", []))})
        row["structured_events_available"] = bool(events)
        row["structured_status"] = "RECOVERED"
    except Exception as error:
        row["structured_error"] = type(error).__name__


assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == PRODUCT
assert not subprocess.check_output(["git", "diff", "HEAD", "--"], text=True)
RESULT["started"] = now()
if len(sys.argv) == 2 and sys.argv[1] == "diagnose":
    phase("terminal_diagnosis", terminal_diagnosis)
    RESULT["status"] = "ORIGINAL_CONSOLE_RECOVERED" if RESULT["phases"]["terminal_diagnosis"]["status"] == "PASS" else "INCOMPLETE"
else:
    phase("original", original)
    phase("consumer_build", build_consumer)
    if RESULT["phases"]["consumer_build"]["status"] == "PASS":
        phase("consumer_build_proof", build_proof)
        phase("consumer_binary", consumer_binary)
        if RESULT["phases"]["original"]["status"] == "PASS":
            phase("amd64", lambda row: image("x86_64", "amd64", 62, row))
            phase("arm64", lambda row: image("aarch64", "arm64", 183, row))
    for name in ("consumer_build_proof", "consumer_binary", "amd64", "arm64"):
        RESULT["phases"].setdefault(name, {"status": "NOT_EXECUTED", "reason": "Required consumer build or original identity unavailable"})
    RESULT["status"] = "PASS" if all(row["status"] == "PASS" for row in RESULT["phases"].values()) else "INCOMPLETE"
RESULT["finished"] = now()
save()
print(json.dumps({"status": RESULT["status"], "phases": {k: v["status"] for k, v in RESULT["phases"].items()}}))
raise SystemExit(RESULT["status"] not in ("PASS", "ORIGINAL_CONSOLE_RECOVERED"))
