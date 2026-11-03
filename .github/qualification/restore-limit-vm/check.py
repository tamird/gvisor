"""Use the maintained guest and reporter; publish only the two restore cases."""

from datetime import datetime, timezone
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
PRODUCT = "a48b7a12cb3bd615c48f9b36d353c02a6463a929"
GUEST = "38745c527784376e0b5d915fe5ebd85e1d1385fd"
SOURCE = os.environ["GITHUB_SHA"]
CASES = ["container/TestRestoreCgroupMemoryLimit/" + name for name in ("createAtLimit", "lowerBeforeRestore")]


def now():
    return datetime.now(timezone.utc).isoformat()


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def save(result):
    OUT.write_text(json.dumps(result, indent=2) + "\n")


def original_preflight(result):
    bundle = TMP / "preflight.zip"
    assert report.file_record(bundle)["sha256"] == "8b037a99ca78cf345b974cd5b1600a109971848affab02320add7ca2b52624d4"
    with ZipFile(bundle) as archive:
        assert archive.namelist() == ["restore-vm-verdict.json"]
        old = json.loads(archive.read(archive.namelist()[0]))
    assert old["product"] == PRODUCT and old["carrier"] == "40af79dce23a11c910a77214b0ae5ec2da01f9cc"
    assert old["status"] == "INCOMPLETE" and old["compile_exit"] is None
    for filename, blob in (("BUILD", "8fdaf8d3db2888f59a2222d2881eb4a7100a7316"),
                           ("payload.sh", "c7edf4a7179600722a36e9994d7c846e0196c216")):
        assert git("hash-object", f"{PACKAGE}/{filename}").decode().strip() == blob
    assert git("rev-parse", "HEAD^").decode().strip() == GUEST
    row = old["stages"]["preflight"]
    assert row["native_exit"] == 0 and row["fresh_local"] and row["delegation_and_guest_exit"]
    # The fixture emits literal XML. The captured byte count and digest bind
    # this reconstruction to what that guest actually returned.
    shell = Path(PACKAGE, "payload.sh").read_text()
    literal = re.findall(r"printf '(<testsuites>[^']*</testsuites>)\\n' > ", shell)
    assert len(literal) == 1
    xml = TMP / "original-preflight.xml"
    xml.write_text(literal[0] + "\n")
    assert report.file_record(xml) == row["xml"]
    counts = report.xml_cases(xml, frozenset({"cgroup2/delegated"}))
    assert counts["required_passed"] == ["cgroup2/delegated"] and (counts["passed"], counts["failed"], counts["skipped"]) == (1, 0, 0)
    result["stages"]["preflight"] = {**row, "status":"PASS", "original_status":old["status"],
                                      "artifact":13226626592, "xml_recovered":True}


def run(stage, result):
    if stage == "preflight":
        original_preflight(result)
        return
    assert git("rev-parse", "HEAD^").decode().strip() == GUEST and git("rev-parse", "HEAD").decode().strip() == SOURCE
    directory = TMP / ("restore-limit-" + stage)
    directory.mkdir()
    row = result["stages"][stage] = {"status": "INCOMPLETE"}
    save(result)
    target = f"//{PACKAGE}:{stage}"
    required = CASES if stage == "restore" else ["cgroup2/delegated"]
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
    if stage == "restore":
        with ZipFile(logs / "test.outputs/outputs.zip") as bundle:
            observed = bundle.read("guest/outputs/capsule-sha256").decode().strip()
            elf = bundle.read("guest/outputs/owner-elf-sha256").decode().strip()
        expected = report.file_record(Path(PACKAGE, "capsule.tar"))["sha256"]
        row["capsule_verified"] = (expected == observed and re.fullmatch(r"[0-9a-f]{64}", elf) is not None
                                   and elf == (TMP/"restore-build/elf.sha256").read_text().strip())
        row["capsule_sha256"] = expected
    if (v["status"] == "PASS" and row["fresh_local"] and not v["skips"]
            and row["delegation_and_guest_exit"] and row.get("capsule_verified", True)):
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
