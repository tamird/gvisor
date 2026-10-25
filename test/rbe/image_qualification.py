import os
from pathlib import Path
import shutil
import sys
if sys.version_info < (3, 11):
    raise RuntimeError("Qualification requires Python 3.11+")
native_bazel = Path(os.environ.pop("QUALIFICATION_NATIVE_BAZEL"))
os.environ["PATH"] = os.environ.pop("QUALIFICATION_NATIVE_PATH")
resolved_bazel = shutil.which("bazel")
if resolved_bazel is None or not native_bazel.samefile(resolved_bazel):
    raise RuntimeError("Pinned interpreter changed the native Bazel wrapper")
"""Generate the new dependency lock remotely without building or testing products."""
import datetime
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import shutil
import tempfile
import time
import traceback
import urllib.request

CONFIG = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
SOURCE = CONFIG["source"]
REPORTER_SOURCE = "9fab2a3456aa646d2b9ea2a4b6e9700640ba7bb9"
REPORTER_HASHES = {'qualification_report.py': '1e86caeb7dfc1807a1781e52cbeac1da03a7c0ab3a613224dcfe579466817c5e', 'unit_matrix.py': '209f47696a8c28e0072a6199d40f85edb855e656195189741ba4cb6215246a37'}
TARGETS = CONFIG["targets"]
OPTIONS = ["--lockfile_mode=error", "--remote_download_outputs=all",
           "--build_event_json_file_path_conversion=false", "--noannounce_rc"]
workspace = Path(os.environ["BUILD_WORKSPACE_DIRECTORY"])
os.chdir(workspace)
if subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != SOURCE:
    raise RuntimeError("Unexpected source revision")
if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"]):
    raise RuntimeError("Tracked source is dirty")
artifacts = Path(os.environ["BUILDBUDDY_ARTIFACTS_DIRECTORY"])
artifacts.mkdir(parents=True, exist_ok=True)
credential = os.environ.get("BUILDBUDDY_API_KEY", "").encode()
if not credential:
    raise RuntimeError("Expected hosted-runner credential for diagnostic redaction")
deadline = float(os.environ["APPLICABILITY_WORK_DEADLINE"])
startup = ["--bazelrc=" + os.environ["APPLICABILITY_AUTH_RC"]]
runner_exit = capture_exit = 0

with tempfile.TemporaryDirectory(prefix="mariadb-lock-preparation-") as temporary:
    private = Path(temporary)
    evidence = private / "qualification-evidence"
    evidence.mkdir()
    for name, expected_hash in REPORTER_HASHES.items():
        url = f"https://raw.githubusercontent.com/tamird/gvisor/{REPORTER_SOURCE}/test/rbe/{name}"
        with urllib.request.urlopen(url, timeout=30) as response:
            body = response.read(2 * 1024 * 1024 + 1)
        if hashlib.sha256(body).hexdigest() != expected_hash:
            raise RuntimeError(f"Pinned reporter identity mismatch: {name}")
        (private / name).write_bytes(body)
    disk_values = {str(path): shutil.disk_usage(path).free for path in (workspace, private, artifacts)}
    (evidence / "disk-capacity.json").write_text(json.dumps(disk_values, indent=2) + "\n")
    if min(disk_values.values()) < 32 * 1024**3:
        raise RuntimeError("Qualification requires at least 32 GiB free on each backing filesystem")
    sys.path.insert(0, str(private))
    import qualification_report as report

    def reject_credentials(data: bytes) -> None:
        values = re.findall(rb"x-buildbuddy-api-key=([^\s'\"]*)", data, re.IGNORECASE)
        if credential in data or any(value != b"[REDACTED]" for value in values):
            raise ValueError("Credential bytes or unrecognized API-key value in qualification evidence")

    def redact_output(path: Path) -> None:
        data = path.read_bytes().replace(credential, b"[REDACTED]")
        reject_credentials(data)
        path.write_bytes(data)

    def replay_output(path: Path, destination) -> None:
        redact_output(path)
        with path.open("rb") as source:
            while chunk := source.read(65536):
                destination.write(chunk)
        destination.flush()

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("Qualification work deadline exhausted")
        return value

    def git_record(path: Path, *arguments: str) -> None:
        path.write_bytes(subprocess.check_output(["git", *arguments], timeout=min(30, remaining())))

    def invoke(name: str, arguments: list[str], kind: str, expected_analysis: tuple[Path, ...] = (), targets: list[str] = TARGETS) -> dict:
        global runner_exit
        preparing_lock = kind == "command" and arguments[:2] == ["mod", "deps"]
        directory = evidence / ("invocation-" + name)
        directory.mkdir()
        raw_events = private / (name + ".events.jsonl")
        events = directory / "events.jsonl"
        command = [*startup, *arguments]
        if kind != "command":
            command.append("--build_event_json_file=" + str(raw_events))
        (directory / "arguments.txt").write_text(json.dumps(command) if kind == "command" else "\n".join(command) + "\n")
        (directory / "expected-targets.txt").write_text("\n".join(targets) + "\n")
        git_record(directory / "commit.txt", "cat-file", "commit", SOURCE)
        git_record(directory / "before.txt", "status", "--porcelain", "--untracked-files=no")
        lock = workspace / "MODULE.bazel.lock"
        if preparing_lock:
            if subprocess.check_output(["git", "ls-files", "--", "MODULE.bazel.lock"], timeout=min(30, remaining())):
                raise RuntimeError("Upstream dependency preparation expected an untracked lock")
            preparation = {"source": SOURCE, "arguments": command,
                           "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                           "lock_before": report.file_record(lock) if lock.is_file() else None,
                           "tracked_lock": False}
            if lock.is_file():
                shutil.copyfile(lock, directory / "lock-before")
        else:
            (directory / "lock-before").write_bytes(lock.read_bytes())
        native_exit = 1
        capture_status = 0
        try:
            with (directory / "stdout.log").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
                native_exit = subprocess.run(["timeout", "--signal=TERM", "--kill-after=5s", str(remaining()), "bazel", *command], stdout=stdout, stderr=stderr).returncode
            if arguments[0] == "query":
                # The exact query stream is retained for the graph consumer.
                redact_output(directory / "stdout.log")
            else:
                replay_output(directory / "stdout.log", sys.stdout.buffer)
            replay_output(directory / "stderr.log", sys.stderr.buffer)
        finally:
            finished = datetime.datetime.now(datetime.timezone.utc).isoformat()
            (directory / "native-exit").write_text(str(native_exit) + "\n")
            if native_exit and (kind != "analysis" or native_exit != 1):
                runner_exit = runner_exit or native_exit
            try:
                # Reporting uses the reserved ten minutes after the work deadline.
                for filename, args in (("after.txt", ["status", "--porcelain", "--untracked-files=no"]), ("head.txt", ["rev-parse", "HEAD"])):
                    (directory / filename).write_bytes(subprocess.check_output(["git", *args], timeout=30))
                if preparing_lock:
                    preparation["lock_after"] = report.file_record(lock) if lock.is_file() else None
                    if lock.is_file():
                        shutil.copyfile(lock, directory / "MODULE.bazel.lock")
                    preparation["tracked_status"] = (directory / "after.txt").read_text()
                else:
                    (directory / "lock-after").write_bytes(lock.read_bytes())
                if kind != "command":
                    captured = report.capture(raw_events, events, output_root, directory / "reports")
                    capture_status = int(bool(captured["errors"]))
            except (OSError, ValueError, subprocess.SubprocessError):
                traceback.print_exc()
                capture_status = 1
            (directory / "capture-exit").write_text(str(capture_status) + "\n")
            context = {"expected_source": SOURCE, "command_kind": kind, "execution_finished_at": finished,
                       "commit_file": "commit.txt", "source_before": "before.txt", "source_after": "after.txt",
                       "final_head": "head.txt", "executed_arguments": "arguments.txt", "native_exit": "native-exit",
                       "capture_exit": "capture-exit"}
            if preparing_lock:
                preparation.update(native_exit=native_exit, execution_finished_at=finished)
                report.write_json(directory / "record.json", preparation)
            else:
                context.update(lock_before="lock-before", lock_after="lock-after")
            report.write_json(directory / "context.json", context)
            if kind == "command":
                result = report.command_verdict(directory / "context.json")
            else:
                result = report.verdict(events, report.manifest_path(events), directory / "context.json",
                                        directory / "expected-targets.txt", expected_analysis)
            report.write_json(directory / "verdict.json", result)
        return result

    try:
        report.write_json(evidence / "authentication.json", {"startup_arguments": startup, "headers": ["remote_header", "bes_header"], "secret_retained": False})
        prepared = invoke("dependency-preparation",
                          ["mod", "deps", "--lockfile_mode=update", "--noannounce_rc"],
                          "command", targets=[])
        if prepared["status"] != "PASS" or not (workspace / "MODULE.bazel.lock").is_file():
            raise RuntimeError("Canonical dependency preparation failed or changed tracked source")

    except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError):
        traceback.print_exc()
        runner_exit = runner_exit or 1
        capture_exit = 1
    finally:
        try:
            for path in evidence.rglob("*"):
                if path.is_file():
                    reject_credentials(path.read_bytes())
            result = report.bundle(evidence, runner_exit, capture_exit, artifacts / "qualification.tar.gz")
            print(json.dumps({"qualification_archive": report.file_record(artifacts / "qualification.tar.gz"), "sources": result["sources"], "status": result["status"]}), flush=True)
            runner_exit = runner_exit or result["native_exit"] or int(result["status"] != "PASS")
        except (OSError, ValueError):
            traceback.print_exc()
            runner_exit = runner_exit or 1
sys.exit(runner_exit)
