"""Recover the original semaphore owner results before running missing owners."""

import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile
import uuid
import xml.etree.ElementTree as ET

import qualification_report as reporter

SOURCE = "14a4e93a872bf6dd49893b6bd9a5f55517167e2b"
OWNERS = [
    "//pkg/sentry/kernel/ipc:ipc_nogo",
    "//pkg/sentry/kernel:kernel_nogo",
    "//pkg/sentry/kernel/semaphore:semaphore_nogo",
    "//pkg/sentry/kernel/semaphore:semaphore_test",
    "//pkg/sentry/syscalls/linux:linux_nogo",
    "//pkg/sentry/socket/control:control_test",
]
MISSING = [OWNERS[4]]
ROOT = Path(os.environ["RUNNER_TEMP"])
OUT = ROOT / "semop-owner-evidence"
OUT.mkdir(exist_ok=True)
KEY = os.environ.get("BUILDBUDDY_API_KEY", "")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write(path, value):
    content = json.dumps(value, indent=2) + "\n"
    assert not KEY or KEY not in content
    path.write_text(content)


def prior():
    path = ROOT / "semop-prior.zip"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == "68f695e858d3691280f7e46d683d5bf9e47fb1e9338de611bc219e5d42c3d429"
    with zipfile.ZipFile(path) as archive:
        base = json.loads(archive.read("original.json"))
        previous = json.loads(archive.read("retry.json"))
    assert base["source"] == previous["source"] == SOURCE
    assert base["priorArtifact"] == 13091282823
    assert base["priorSHA256"] == "5e54e4170122fb66ad56d3b7d300d79a7ce84ab854303baa7dcab64992093794"
    kernel = OWNERS[1]
    assert set(base["passed"]) == set(OWNERS) - {kernel, *MISSING}
    assert len(previous["executed"]) == 1
    attempt = previous["executed"][0]
    assert attempt["owner"]["label"] == kernel and attempt["status"] == "PASSED"
    assert attempt["xml"]["passed"] == 1 and not attempt["xml"]["failed"] and not attempt["xml"]["skipped"]
    assert not attempt["cached_locally"] and attempt["execution_info"] == {"strategy": "remote"}
    assert previous["architectureMarkers"] == {kernel: ["amd64", "arm64"]}
    assert {entry["label"] for entry in previous["missing"]} == set(MISSING)
    row = {"status": "INCOMPLETE", "source": SOURCE, "passed": [*base["passed"], kernel], "missing": MISSING,
           "priorArtifacts": [13091282823, 13099715950], "actionDiagnosticArtifacts": [13096994117, 13098968702],
           "priorSHA256": digest, "previousBazelExit": previous["bep_finish"]["exitCode"]}
    write(OUT / "original.json", row)
    (ROOT / "semop-missing.txt").write_text("\n".join(MISSING) + "\n")


def retry():
    missing = (ROOT / "semop-missing.txt").read_text().split()
    assert missing == MISSING
    original = json.loads((OUT / "original.json").read_text())
    assert missing == original["missing"]
    private = ROOT / "semop-owner-private"
    private.mkdir(exist_ok=True)
    def git(*args):
        return subprocess.check_output(["git", "-c", "safe.directory=" + str(Path.cwd()), *args])
    assert git("rev-parse", "HEAD").decode().strip() == SOURCE
    (private / "commit").write_bytes(git("cat-file", "commit", SOURCE))
    (private / "before").write_bytes(git("diff", "HEAD", "--"))
    assert not (private / "before").read_bytes()
    invocation = str(uuid.uuid4())
    write(OUT / "invocation.json", {"source": SOURCE, "id": invocation, "expected": missing, "at": now()})
    print("Bazel invocation", invocation, flush=True)
    args = ["test", "--invocation_id=" + invocation,
            "--local_resources=cpu=1",
            "--strategy=GoStaticAnalysis=local", "--config=x86_64", "--jobs=4", "--keep_going", "--build_tag_filters=", "--test_tag_filters=",
            "--noremote_local_fallback", "--strategy=TestRunner=remote", "--test_timeout=300", "--test_output=errors", "--nocache_test_results",
            "--modify_execution_info=^TestRunner$=+no-remote-cache", "--test_env=GO_TEST_WRAP_TESTV=1",
            "--remote_download_outputs=toplevel", "--remote_download_regex=.*[/]test[.](log|xml)$",
            "--profile=" + str(private / "profile.gz"), "--build_event_json_file_path_conversion=false",
            "--build_event_json_file=" + str(private / "raw.json"), *missing]
    (private / "arguments").write_text("\n".join(args) + "\n")
    write(OUT / "retry.json", {"status": "INCOMPLETE", "source": SOURCE, "expected": missing, "started_at": now()})
    with (private / "bazel.log").open("wb") as log:
        rc = subprocess.run(["timeout", "--signal=INT", "--kill-after=30s", "55m", "bazel", "--noblock_for_lock",
                             "--bazelrc=" + str(ROOT / "tcp.bazelrc"), *args], stdout=log, stderr=log).returncode
    finished = now()
    profile_summary = {}
    profile = private / "profile.gz"
    if profile.exists():
        try:
            with gzip.open(profile, "rt") as stream:
                trace = json.load(stream)
            for event in trace.get("traceEvents", []):
                name = str(event.get("name", ""))
                for label in ("//pkg/sentry/socket/netstack:netstack", "//pkg/sentry/socket:socket",
                              "//pkg/sentry/socket/netfilter:netfilter", "//pkg/sentry/syscalls/linux:linux"):
                    if "Analyzing" in name and label in name:
                        phase = event.get("cat", "")
                        if re.fullmatch(r"[a-zA-Z _-]{1,70}", phase):
                            counts = profile_summary.setdefault(label, {})
                            counts[phase] = counts.get(phase, 0) + 1
        except (OSError, ValueError):
            profile_summary = {"status": "incomplete profile"}
    write(OUT / "action-profile.json", profile_summary)
    # Expose action names/timing and process counts, never complete commands.
    with (private / "bazel.log").open(errors="replace") as stream:
        last = []
        processes = []
        for line in stream:
            if "processes:" in line:
                processes = re.findall(r"[0-9]+ (?:remote cache hit|remote|local|internal|worker)", line)
            if "Analyzing " in line or "Generating " in line:
                last.append({"owners": re.findall(r"//[a-z0-9_./-]+:[a-z0-9_./-]+", line),
                             "balance": re.findall(r"\[[0-9,]+ / [0-9,]+\]", line),
                             "elapsed": re.findall(r"\b[0-9]+s\b", line),
                             "runners": re.findall(r"\b(?:local|remote|worker)\b", line)})
                last = last[-40:]
    write(OUT / "progress.json", {"processes": processes, "last": last})
    (private / "exit").write_text(str(rc))
    write(OUT / "retry.json", {"status": "FAIL" if rc else "INCOMPLETE", "source": SOURCE,
                               "expected": missing, "native_exit": rc, "execution_finished_at": finished})
    (private / "after").write_bytes(git("diff", "HEAD", "--"))
    (private / "head").write_bytes(git("rev-parse", "HEAD"))
    captured = reporter.capture(private / "raw.json", private / "bep.json", Path("/"), private / "reports")
    (private / "capture-exit").write_text(str(int(bool(captured["errors"]))))
    context = {"expected_source": SOURCE, "commit_file": "commit", "source_before": "before", "source_after": "after",
               "final_head": "head", "executed_arguments": "arguments", "native_exit": "exit", "capture_exit": "capture-exit",
               "execution_finished_at": finished, "command_kind": "test"}
    write(private / "context.json", context)
    (private / "expected.txt").write_text("\n".join(missing) + "\n")
    result = reporter.verdict(private / "bep.json", reporter.manifest_path(private / "bep.json"), private / "context.json", private / "expected.txt")
    for attempt in result["executed"]:
        details = attempt["execution_info"]
        attempt["execution_info"] = {key: details[key] for key in ("strategy", "cachedRemotely") if key in details}
        if attempt["cached_locally"] or details.get("cachedRemotely", False) or details.get("strategy") != "remote":
            result["errors"].append("Retry lacks fresh remote test execution")
            if result["status"] == "PASS":
                result["status"] = "INCOMPLETE"
    markers = {}
    for record in captured["reports"]:
        if record["name"] == "test.log":
            label = record["identity"]["label"]
            log = (private / record["path"]).read_bytes()
            markers[label] = [arch for arch in ("amd64", "arm64") if arch.encode() + b"..." in log]
        if record["name"] == "test.xml":
            source = private / record["path"]
            shutil.copyfile(source, OUT / ("retry-" + record["sha256"] + ".xml"))
    result["architectureMarkers"] = markers
    if markers != dict.fromkeys(MISSING, ["amd64", "arm64"]):
        result["errors"].append("Missing architecture markers in original TestRunner logs")
        if result["status"] == "PASS":
            result["status"] = "INCOMPLETE"
    write(OUT / "retry.json", result)
    print("Retry", result["status"], result["case_counts"], flush=True)


def finish():
    original = json.loads((OUT / "original.json").read_text())
    retry_result = json.loads((OUT / "retry.json").read_text()) if (OUT / "retry.json").exists() else None
    failed = original["status"] == "FAIL" or retry_result and retry_result["status"] == "FAIL"
    complete = original["status"] == "PASS" or original["status"] == "INCOMPLETE" and retry_result and retry_result["status"] == "PASS"
    result = {"status": "FAIL" if failed else "PASS" if complete else "INCOMPLETE", "source": SOURCE,
              "expected": OWNERS, "original": original, "retry": retry_result, "verified_at": now()}
    write(OUT / "verdict.json", result)
    print(result["status"], flush=True)
    return result["status"] != "PASS"


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "finish":
        sys.exit(finish())
    try:
        {"prior": prior, "retry": retry}[mode]()
    except Exception as err:
        write(OUT / (mode + "-error.json"), {"status": "INCOMPLETE", "error_type": type(err).__name__, "verified_at": now()})
        raise SystemExit(1)
