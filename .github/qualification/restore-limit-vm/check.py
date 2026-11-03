"""Measure the native checkpoint call on matched encoder builds in a single guest."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from zipfile import ZipFile

sys.path.insert(0, str(Path("test/rbe").resolve()))
from credential_artifacts import prepare
import qualification_report as report


TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "restore-vm-verdict.json"
PACKAGE = ".github/qualification/restore-limit-vm"
GUEST = "b343642cb16de74e01312ac2e0895f13a8aebcbb"
SOURCE = os.environ["GITHUB_SHA"]
ARMS = ("stock", "fixed")
CASES = ["container/TestCheckpointTmpfsTreeForProfile"]


def now():
    return datetime.now(timezone.utc).isoformat()


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def save(result):
    OUT.write_text(json.dumps(result, indent=2) + "\n")


def originals(bundle, row):
    """Check each real Go test result, including runs excluded from timing."""
    def small(name, ceiling=8192):
        key = "guest/outputs/" + name
        info = bundle.getinfo(key)
        assert info.file_size <= ceiling
        return bundle.read(key)

    files = ("checkpoint.img", "pages.img", "pages_meta.img")
    row["arms"] = {}
    row["guest_cpus"] = small("cpus", 80).decode().strip()
    row["guest_affinity"] = small("affinity", 80).decode().strip()
    assert row["guest_affinity"] == row["guest_cpus"] == "0-1"
    for arm in ARMS:
        owner, runtime, sentry, capsule = small(arm + ".sha", 300).decode().splitlines()
        assert all(re.fullmatch(r"[0-9a-f]{64}", x) for x in (owner, runtime, sentry, capsule))
        assert owner == (TMP / "live-build" / arm / "elf.sha256").read_text().strip()
        assert capsule == report.file_record(Path(PACKAGE, arm + ".tar"))["sha256"]
        row["arms"][arm] = {"owner": owner, "runsc": runtime, "sentry": sentry, "capsule": capsule}
    # The release launcher may be unchanged: the Go code lives in its Sentry sidecar.
    assert row["arms"]["stock"]["sentry"] != row["arms"]["fixed"]["sentry"]
    samples = row["measurements"] = []
    original = TMP / "pair.xml"
    for i in range(17):
        arms = ARMS if i == 0 or i % 2 else tuple(reversed(ARMS))
        for arm in arms:
            prefix = f"r{i}-{arm}"
            code = int(small(prefix + ".exit", 8))
            sample = {"round": i, "arm": arm, "exit": code}
            samples.append(sample)
            xml = small(prefix + ".xml", 2097152)
            original.write_bytes(xml)
            sample["xml_sha256"] = hashlib.sha256(xml).hexdigest()
            counts = report.xml_cases(original, frozenset(CASES))
            assert code == 0 and counts["required_passed"] == CASES
            assert (counts["passed"], counts["failed"], counts["skipped"]) == (1, 0, 0)
            lines = small(prefix + ".markers").decode().splitlines()
            expected = {"TMPFS-CAP save 2147483648", "TMPFS-CAP restore 2147483648",
                        "TMPFS-GRAPH kernel=true", "TMPFS-VERIFIED 50000"}
            times = {}
            for line in lines:
                if m := re.fullmatch(r"TMPFS-NS (checkpoint|new|restore|new-to-guest) ([1-9][0-9]*)", line):
                    assert m[1] not in times
                    times[m[1]] = int(m[2])
                else:
                    expected.remove(line)
            assert not expected and set(times) == {"checkpoint", "new", "restore", "new-to-guest"}
            assert times["new-to-guest"] > times["restore"]
            sizes = dict(zip(files, map(int, small(prefix + ".bytes", 100).splitlines()), strict=True))
            assert all(x > 0 for x in sizes.values())
            sample.update(checkpoint_ns=times["checkpoint"], archive_bytes=sizes, restored_files=50000)
    for arm in ARMS:
        lines = []
        for x in samples:
            if x["round"] and x["arm"] == arm:
                lines.append(f"BenchmarkNativeCheckpoint 1 {x['checkpoint_ns']} ns/op {sum(x['archive_bytes'].values())} archive-B {x['archive_bytes'][files[0]]} kernel-file-B")
        assert len(lines) == 16
        (TMP / (arm + ".bench")).write_text("\n".join(lines) + "\n")


def run(stage, result):
    assert git("rev-parse", "HEAD^").decode().strip() == GUEST and git("rev-parse", "HEAD").decode().strip() == SOURCE
    directory = TMP / ("restore-limit-" + stage)
    directory.mkdir()
    row = result["stages"][stage] = {"status": "INCOMPLETE"}
    save(result)
    target = f"//{PACKAGE}:{stage}"
    required = CASES
    (directory / "owners").write_text(target + "\n")
    report.write_json(directory / "required.json", {target: required})
    raw = directory / "raw.jsonl"
    args = ["test", "--config=rbe", "--config=rbe-local-tests", "--//tools/bazeldefs:local_test_architecture=amd64",
            "--jobs=32", "--nocache_test_results", "--noremote_upload_local_results", "--bes_backend=",
            "--zip_undeclared_test_outputs", "--test_timeout=900", "--build_event_json_file_path_conversion=false",
            f"--build_event_json_file={raw}", target]
    with tempfile.TemporaryDirectory() as private:
        # This is only the coordinator. The maintained guest launcher forwards
        # named test variables and exposes just its input and result directories.
        prepare(Path(private))
        rc = Path(private) / "private.bazelrc"
        rc.write_text(f"common --credential_helper=remote.buildbuddy.io={private}/helper\n"
                      "build:buildbuddy_remote_executor --remote_executor=grpcs://remote.buildbuddy.io\n"
                      "build:buildbuddy_remote_executor --remote_cache=grpcs://remote.buildbuddy.io\n")
        env = {k: v for k, v in os.environ.items() if k != "BUILDBUDDY_API_KEY"}
        (directory / "executed_arguments").write_text("\n".join(args) + "\n")
        (directory / "commit_file").write_bytes(git("cat-file", "commit", SOURCE))
        (directory / "source_before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
        with (directory / "private-console").open("wb") as console:
            original = subprocess.run(["bazel", f"--bazelrc={rc}", *args], env=env, stdout=console, stderr=subprocess.STDOUT)
        row.update(native_exit=original.returncode, finished_utc=now())
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
    (attempt,) = v["executed"]
    row["fresh_local"] = (attempt["owner"]["label"] == target and not attempt["cached_locally"]
                          and not attempt["execution_info"].get("cachedRemotely")
                          and attempt["execution_info"].get("strategy") == "local")
    logs = Path("bazel-testlogs", PACKAGE, stage)
    log = (logs / "test.log").read_text(errors="replace")
    row["delegation_and_guest_exit"] = "GVISOR_RESTORE_DELEGATED_MEMORY=5368709120" in log and "phase=guest_status_read status=0" in log
    row["xml"] = report.file_record(logs / "test.xml")
    direct = report.xml_cases(logs / "test.xml", frozenset(required))
    row["original_xml"] = {k:direct[k] for k in ("passed", "failed", "skipped", "required_passed")}
    row["log"] = report.file_record(logs / "test.log")
    row["diagnostic_classes"] = [name for name, expression in {
        "oom_kill":r"^oom_kill [1-9]\d*$", "permission":r"permission denied|operation not permitted",
        "restore":r"restoring container under", "fixture":r"FindFile\(test_app\)|creating container:",
    }.items() if re.search(expression, log, re.M | re.I)]
    with ZipFile(logs / "test.outputs/outputs.zip") as bundle:
        originals(bundle, row)
    if (v["status"] == "PASS" and row["fresh_local"] and not v["skips"]
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
    state["status"] = state["stages"][stage]["status"] if stage == "restore" else "INCOMPLETE"
    save(state)
    sys.exit(state["stages"][stage]["status"] != "PASS")
