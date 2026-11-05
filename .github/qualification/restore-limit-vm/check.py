"""Use the maintained guest and reporter; publish only the two restore cases."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zipfile import ZipFile

sys.path.insert(0, str(Path("test/rbe").resolve()))
import qualification_report as report


TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "restore-vm-verdict.json"
PACKAGE = ".github/qualification/restore-limit-vm"
PRODUCT = "c31b2adc27b1d19b1e647b3239cd2b3b536a5adf"
GUEST = "38745c527784376e0b5d915fe5ebd85e1d1385fd"
SOURCE = os.environ["GITHUB_SHA"]
CASES = ["native/matchedSave"]


def now():
    return datetime.now(timezone.utc).isoformat()


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def save(result):
    OUT.write_text(json.dumps(result, indent=2) + "\n")


def host_diagnostic(console, events):
    # This command uses only trusted, pinned VM inputs. Keep short Bazel
    # errors, omitting URLs, host/user directories and sensitive strings.
    import hashlib
    result = {"console_sha256": report.file_record(console)["sha256"], "errors": [], "bep_codes": [], "invalid_bep_rows": 0}
    def record(text):
        for original in text.splitlines():
            line = original.strip()
            if not re.search(r"^(?:ERROR|FATAL)[: ]|no such (?:package|target)|failed to fetch|IOException|PKIX|not a declared prerequisite", line, re.I):
                continue
            digest = hashlib.sha256(line.encode()).hexdigest()
            if any(row["sha256"] == digest for row in result["errors"]):
                continue
            line = re.sub(r"(?i)(?:https?|grpcs?|file)://\S+", "<uri>", line)
            line = re.sub(r"(?<![A-Za-z0-9_.:/-])/(?:home|private|Users|tmp|var|etc|opt|usr|runner|__w)/[^\s:()'\"]+", "<host-path>", line)
            if re.search(r"token|password|credential|authorization|secret|bearer|api.?key|github_pat_|gh[pousr]_|[A-Za-z0-9+/=_-]{40,}", line, re.I):
                line = "<screened>"
            if len(result["errors"]) < 5:
                result["errors"].append({"sha256": digest, "line": line[:440]})
    with console.open("rb") as stream:
        for line in stream:
            record(line[:8192].decode(errors="replace"))
    if events.is_file():
        for line in events.open("rb"):
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                result["invalid_bep_rows"] += 1
                continue
            for part in ("finished", "aborted", "action"):
                entry = event.get(part, {})
                detail = entry.get("failureDetail") or {}
                if detail:
                    for key, value in detail.items():
                        if isinstance(value, dict) and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{1,50}", key):
                            code = value.get("code")
                            if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,90}", code):
                                if [key, code] not in result["bep_codes"] and len(result["bep_codes"]) < 5:
                                    result["bep_codes"].append([key, code])
                    record(detail.get("message", ""))
                if part == "aborted":
                    record(entry.get("description", ""))
    return result


def run(stage, result):
    assert stage in ("preflight", "restore") and result["product"] == PRODUCT
    if stage == "restore":
        assert result["stages"]["preflight"]["status"] == "PASS"
    assert git("rev-parse", "HEAD^").decode().strip() == "3da1a3fcdf66ad21987cad1be1fa5b81f882df55"
    assert git("merge-base", "--is-ancestor", GUEST, SOURCE) == b""
    directory = TMP / ("restore-limit-" + stage)
    directory.mkdir()
    row = result["stages"][stage] = {"status": "INCOMPLETE"}
    save(result)
    target = f"//{PACKAGE}:{stage}"
    required = ["cgroup2/delegated"] if stage == "preflight" else CASES
    (directory / "owners").write_text(target + "\n")
    report.write_json(directory / "required.json", {target: required})
    raw = directory / "raw.jsonl"
    args = ["test", "--config=x86_64", "--jobs=3", "--local_test_jobs=1",
            "--strategy=TestRunner=local", "--modify_execution_info=TestRunner=-no-local",
            "--remote_executor=", "--remote_cache=", "--bes_backend=",
            "--test_tag_filters=", "--build_tag_filters=", "--build_tests_only=false",
            "--nocache_test_results", "--flaky_test_attempts=1", "--zip_undeclared_test_outputs",
            f"--test_timeout={900 if stage == 'preflight' else 2700}", "--build_event_json_file_path_conversion=false",
            f"--distdir={TMP / 'ubuntu-distdir'}", f"--downloader_config={TMP / 'ubuntu-mirrors'}",
            f"--build_event_json_file={raw}", target]
    (directory / "executed_arguments").write_text("\n".join(args) + "\n")
    (directory / "commit_file").write_bytes(git("cat-file", "commit", SOURCE))
    (directory / "source_before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    bazel = TMP / "restore-tools/bazel"
    assert subprocess.check_output([bazel, "--version"], text=True).strip() == "bazel 8.8.1"
    with (directory / "private-console").open("wb") as console:
        original = subprocess.run(["timeout", "-k", "20", "2040" if stage == "preflight" else "3300", bazel, "--nosystem_rc", "--nohome_rc", *args],
                                  stdout=console, stderr=subprocess.STDOUT)
    row.update(native_exit=original.returncode, finished_utc=now())
    save(result)
    row["host_diagnostic"] = host_diagnostic(directory / "private-console", raw)
    save(result)
    (directory / "native_exit").write_text(str(original.returncode))
    (directory / "source_after").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (directory / "final_head").write_bytes(git("rev-parse", "HEAD"))
    events = directory / "events.jsonl"
    captured = report.capture(raw, events, Path("bazel-out"), directory / "reports", expected_kind="test")
    (directory / "capture_exit").write_text(str(int(bool(captured["errors"]))))
    keys = "executed_arguments commit_file source_before source_after native_exit capture_exit final_head".split()
    report.write_json(directory / "context.json", {**{k:k for k in keys}, "expected_source":SOURCE,
                      "command_kind":"test", "execution_finished_at":row["finished_utc"]})
    v = report.verdict(events, report.manifest_path(events), directory / "context.json", directory / "owners",
                       case_manifest=directory / "required.json")
    row.update(qualified_status=v["status"], counts=v["case_counts"], missing_owners=len(v["missing"]),
               capture_errors=len(captured["errors"]), verdict_errors=len(v["errors"]), verdict_failures=len(v["failures"]),
               error_classes=[label for label, pattern in {"local_file":"Expected a local report URI",
                   "capture_bound":"is not in the subpath|retention bounds|No such file|Expected one original",
                   "source":"dirty|source HEAD", "zero_cases":"no executed named cases",
                   "xml":"XML declares|Unexpected XML", "owner":"Expected target|coverage is incomplete",
               }.items() if any(re.search(pattern, str(e), re.I) for e in [*captured["errors"], *v["errors"]])],
               nonpassing_required=[r["name"] for r in v["unpassed_required_cases"] if r["name"] in required])
    row["action_failures"] = []
    for line in raw.open():
        event = json.loads(line)
        if "action" in event and not event["action"].get("success", False):
            action = event["action"]
            identifier = event.get("id", {}).get("actionCompleted", {})
            label, mnemonic = identifier.get("label", ""), action.get("type", "")
            if re.fullmatch(r"[@+A-Za-z0-9_./:~=-]{1,240}", label) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", mnemonic):
                row["action_failures"].append([label, mnemonic, action.get("exitCode")])
        if len(row["action_failures"]) == 4:
            break
    save(result)
    (attempt,) = v["executed"]
    row["fresh_local"] = (attempt["owner"]["label"] == target and not attempt["cached_locally"]
                          and not attempt["execution_info"].get("cachedRemotely")
                          and attempt["execution_info"].get("strategy") == "local")
    logs = Path("bazel-testlogs", PACKAGE, stage)
    log = (logs / "test.log").read_text(errors="replace")
    row["delegation_and_guest_exit"] = "GC_SAVE_VALIDATED_MEMORY=2147483648" in log and "phase=guest_status_read status=0" in log
    row["host_phases"] = re.findall(r"^host_uptime_seconds=[0-9.]+ phase=([a-z_]{1,32}) status=(-|[0-9]{1,3}) outer_test_timeout_seconds=(?:900|2700)$", log, re.M)[:24]
    row["image_size"] = re.findall(r"^Payload filesystem template: (\d{1,12}) bytes; scratch capacity: (\d{1,2}) GiB$", log, re.M)[:1]
    row["guest_work_fs"] = re.findall(r"^Guest work filesystem: block_size=(\d{1,6}) blocks=(\d{1,12})\s*$", log, re.M)[:1]
    row["fixture_errors"] = [kind for kind in ("Payload is missing or not executable", "No space left on device",
        "Read-only file system", "Permission denied", "Operation not permitted", "No such file or directory",
        "Input/output error", "Kernel panic", "Timed out", "exceeds scratch capacity")
        if kind.lower() in log.lower()]
    save(result)
    row["xml"] = report.file_record(logs / "test.xml")
    direct = report.xml_cases(logs / "test.xml", frozenset(required))
    row["original_xml"] = {k:direct[k] for k in ("passed", "failed", "skipped", "required_passed")}
    row["log"] = report.file_record(logs / "test.log")
    row["diagnostic_classes"] = [name for name, expression in {
        "oom_kill":r"^oom_kill [1-9]\d*$", "permission":r"permission denied|operation not permitted",
        "restore":r"restoring container under", "fixture":r"FindFile\(test_app\)|creating container:",
    }.items() if re.search(expression, log, re.M | re.I)]
    row["oom_kill_counts"] = [int(n) for n in re.findall(r"^\s*oom_kill ([0-9]{1,9})\s*$", log, re.M)[:8]]
    row["test_error_locations"] = sorted(set(re.findall(r"\b(container_save_restore_test\.go:\d{1,7})", log)))[:8]
    save(result)
    if stage == "restore":
        import paired
        with ZipFile(logs / "test.outputs/outputs.zip") as bundle:
            paired.collect(bundle, row, result, save)
    # The preflight emits one literal XML case, byte-identical to the original
    # verified guest result. The restore cases are the authored Go test XML.
    if stage == "preflight":
        row["source_clean"] = (not (directory / "source_before").read_bytes().strip()
                               and not (directory / "source_after").read_bytes().strip()
                               and (directory / "final_head").read_text().strip() == SOURCE)
        completed = (row["source_clean"] and not captured["errors"] and row["xml"] == {"bytes": 107, "sha256": "936d97fd5273fe8167548a4ba73bf7e1be51323d908891c660509c30b3a3ae77"}
                     and direct["required_passed"] == required and direct["passed"] == 1
                     and direct["failed"] == direct["skipped"] == 0 and not v["missing"])
    else:
        completed = (not captured["errors"] and not v["missing"] and direct["required_passed"] == required
                     and (direct["passed"], direct["failed"], direct["skipped"]) == (1, 0, 0)
                     and row.get("verified_samples") == 26)
    if (original.returncode == 0 and completed and row["fresh_local"] and not v["skips"]
            and row["delegation_and_guest_exit"]):
        row["status"] = "PASS"
    elif original.returncode:
        row["status"] = "FAIL"
    row["verified_utc"] = now()


if __name__ == "__main__":
    stage = sys.argv[1]
    state = json.loads(OUT.read_text())
    try:
        run(stage, state)
    except Exception as error:
        row = state["stages"].setdefault(stage, {})
        row.update(status="FAIL" if row.get("native_exit") not in (None, 0) else "INCOMPLETE", reporter_error=type(error).__name__)
    state["status"] = state["stages"][stage]["status"] if stage == "restore" or state["stages"][stage]["status"] == "FAIL" else "INCOMPLETE"
    save(state)
    sys.exit(state["stages"][stage]["status"] != "PASS")
