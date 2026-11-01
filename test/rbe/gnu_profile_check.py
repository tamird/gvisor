"""Check the reusable GNU profile through one ordinary analyzer owner."""

import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import traceback
import uuid

from image_execution import execution_records
import qualification_report as reporter


SOURCE = "14a4e93a872bf6dd49893b6bd9a5f55517167e2b"
TARGET = "//pkg/waiter:waiter_nogo"
PROPERTIES = {
    "Arch": "amd64",
    "EstimatedMemory": "2GB",
    "OSFamily": "linux",
    "container-image": "docker://" + os.environ["GNU"],
    "workload-isolation-type": "firecracker",
}
ROOT = Path(os.environ["RUNNER_TEMP"])
PRIVATE = ROOT / "gnu-profile-private"
OUT = ROOT / "gnu-profile-evidence"
PRIVATE.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)


def now():
    return datetime.now(timezone.utc).isoformat()


def write(value):
    (OUT / "verdict.json").write_text(json.dumps(value, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", "-c", "safe.directory=" + str(Path.cwd()), *args])


def qualify(result):
    assert git("rev-parse", "HEAD").decode().strip() == SOURCE
    assert not git("diff", "HEAD", "--")
    bazel = ["bazel", "--noblock_for_lock", "--bazelrc=" + str(ROOT / "gnu.bazelrc")]
    query = PRIVATE / "platform.txt"
    with query.open("wb") as stdout, (PRIVATE / "query.log").open("wb") as stderr:
        code = subprocess.run(["timeout", "180", *bazel, "query",
                               "--inject_repository=tcp_gnu=" + str(ROOT / "gnu-platform"),
                               "@tcp_gnu//:gnu", "--output=build"],
                              stdout=stdout, stderr=stderr).returncode
    result["query_exit"] = code
    write(result)
    assert code == 0
    properties = ast.literal_eval(re.search(r"exec_properties = (\{.*?\})", query.read_text(), re.S)[1])
    assert properties == PROPERTIES
    result["configured_properties"] = properties
    result["profile_files"] = {
        name: hashlib.sha256((ROOT / "gnu-platform" / name).read_bytes()).hexdigest()
        for name in ("BUILD.bazel", "MODULE.bazel")
    }
    (PRIVATE / "commit").write_bytes(git("cat-file", "commit", SOURCE))
    (PRIVATE / "before").write_bytes(git("diff", "HEAD", "--"))
    invocation = str(uuid.uuid4())
    result["invocation"] = invocation
    args = ["test", "--invocation_id=" + invocation, "--config=x86_64", "--jobs=42",
            "--build_tag_filters=", "--test_tag_filters=", "--nocache_test_results",
            "--modify_execution_info=^TestRunner$=+no-remote-cache", "--test_timeout=300",
            "--test_output=errors", "--strategy=TestRunner=remote", "--test_env=GO_TEST_WRAP_TESTV=1",
            "--remote_download_outputs=toplevel", "--remote_download_regex=.*[/]test[.](log|xml)$",
            "--build_event_json_file_path_conversion=false",
            "--build_event_json_file=" + str(PRIVATE / "raw.json"),
            "--execution_log_json_file=" + str(PRIVATE / "spawns.json"), TARGET]
    (PRIVATE / "arguments").write_text("\n".join(args) + "\n")
    result["phase"] = "test"
    result["started"] = now()
    write(result)
    with (PRIVATE / "test.log").open("wb") as log:
        code = subprocess.run(["timeout", "--signal=INT", "--kill-after=30s", "20m", *bazel, *args],
                              stdout=log, stderr=log).returncode
    result.update(native_exit=code, execution_finished=now(), status="FAIL" if code else "INCOMPLETE")
    write(result)
    (PRIVATE / "exit").write_text(str(code))
    (PRIVATE / "after").write_bytes(git("diff", "HEAD", "--"))
    (PRIVATE / "head").write_bytes(git("rev-parse", "HEAD"))
    captured = reporter.capture(PRIVATE / "raw.json", PRIVATE / "bep.json", Path("/"), PRIVATE / "reports")
    (PRIVATE / "capture-exit").write_text(str(int(bool(captured["errors"]))))
    context = {
        "expected_source": SOURCE, "commit_file": "commit", "source_before": "before",
        "source_after": "after", "final_head": "head", "executed_arguments": "arguments",
        "native_exit": "exit", "capture_exit": "capture-exit", "command_kind": "test",
        "execution_finished_at": result["execution_finished"],
    }
    (PRIVATE / "context.json").write_text(json.dumps(context))
    (PRIVATE / "expected.txt").write_text(TARGET + "\n")
    verdict = reporter.verdict(PRIVATE / "bep.json", reporter.manifest_path(PRIVATE / "bep.json"),
                               PRIVATE / "context.json", PRIVATE / "expected.txt")
    result["qualification"] = {key: verdict[key] for key in
                               ("status", "case_counts", "errors", "missing", "bep_finish")}
    if verdict["status"] == "FAIL":
        result["status"] = "FAIL"
    write(result)
    rows = []
    result["owner_actions"] = rows
    with (PRIVATE / "spawns.json").open() as stream:
        for _, action in execution_records(stream):
            if action is None or action.get("targetLabel") not in (TARGET, "//pkg/waiter:waiter"):
                continue
            if action["mnemonic"] not in ("TestRunner", "GoStaticAnalysis"):
                continue
            props = {item["name"]: item["value"] for item in action["platform"]["properties"]}
            row = {key: action.get(key) for key in ("targetLabel", "mnemonic", "runner", "cacheHit", "digest")}
            row.update(exit=action.get("exitCode", 0), status=action.get("status", ""),
                       properties={key: props.get(key) for key in PROPERTIES})
            if action["mnemonic"] == "TestRunner":
                args = action["commandArgs"]
                row["executable"] = Path(args[0]).name
                assert row["executable"] in ("test-setup.sh", "generate-xml.sh")
                if row["executable"] == "test-setup.sh":
                    assert Path(args[1]).name == "waiter_nogo"
            rows.append(row)
            write(result)
            assert row["properties"] == PROPERTIES
            assert row["exit"] == 0 and not row["status"]
    result["owner_actions"] = rows
    write(result)
    tests = [row for row in rows if row.get("executable") == "test-setup.sh"]
    xml = [row for row in rows if row.get("executable") == "generate-xml.sh"]
    analyses = [row for row in rows if row["mnemonic"] == "GoStaticAnalysis"]
    assert len(tests) == 1 and tests[0]["runner"] == "remote" and not tests[0]["cacheHit"]
    assert len(xml) <= 1
    assert len(analyses) == 4
    assert all(row["cacheHit"] or row["runner"] == "remote" for row in analyses)
    assert sum(row["targetLabel"] == TARGET for row in analyses) == 2
    logs = [record for record in captured["reports"] if record["name"] == "test.log"]
    assert len(logs) == 1 and logs[0]["identity"]["label"] == TARGET
    log = (PRIVATE / logs[0]["path"]).read_bytes()
    result["analyzer_architectures"] = [arch for arch in ("amd64", "arm64") if arch.encode() + b"..." in log]
    assert result["analyzer_architectures"] == ["amd64", "arm64"]
    assert code == 0 and verdict["status"] == "PASS" and not verdict["case_counts"]["skipped"]
    result["status"] = "PASS"


if __name__ == "__main__":
    result = {"status": "INCOMPLETE", "phase": "bootstrap", "source": SOURCE, "expected": [TARGET],
              "kernel_replay_artifact": 13099588299}
    write(result)
    try:
        qualify(result)
    except Exception as err:
        result["collection_error"] = type(err).__name__
        result["error_line"] = traceback.extract_tb(err.__traceback__)[-1].lineno
    result["verified"] = now()
    write(result)
    print(result["status"], flush=True)
    raise SystemExit(result["status"] != "PASS")
