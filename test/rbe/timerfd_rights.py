"""Run the original timerfd syscall test owners and retain their actual observations."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import traceback
import uuid
import xml.etree.ElementTree as ET
import zipfile

from image_execution import execution_records
import qualification_report as reporter


ROOT = Path(os.environ["RUNNER_TEMP"])
OUT = ROOT / "timer-private"
PUBLIC = ROOT / "timer-public" / "verdict.json"
SOURCE = os.environ["FIXTURE"]
BASE = os.environ["BASE"]
CASE = "TimerfdRightsTest.RearmQueuedTimer"
LABEL = "//test/syscalls:timerfd_test_"
TARGETS = [LABEL + variant for variant in ("native", "runsc_systrap_directfs", "runsc_systrap_directfs_save")]
MARKERS = {
    "start": b"TIMER_RIGHTS queued-only reference established",
    "control_read": b"TIMER_RIGHTS installed control expired once; arming queued timer",
    "queued_read": b"TIMER_RIGHTS queued timer expired once",
    "panic": b"panic:",
    "nil_pointer": b"invalid memory address or nil pointer dereference",
    "nil_timer": b"time: Reset called on uninitialized Timer",
    "Reset": b"time.(*Timer).Reset(",
    "resetKickerLocked": b"(*SampledTimer).resetKickerLocked(",
    "SetWithClock": b"(*SampledTimer).SetWithClock(",
    "TimerfdSettime": b".TimerfdSettime(",
    "restore": b" restore --image-path",
    "after_restore_error": b"after restore error:",
}
PROPERTIES = {
    "Arch": "amd64", "OSFamily": "linux", "EstimatedMemory": "2GB",
    "container-image": "docker://" + os.environ["GNU"], "workload-isolation-type": "firecracker",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-c", "safe.directory=" + str(Path.cwd()), *args])


def save(result: dict) -> None:
    PUBLIC.write_text(json.dumps(result, indent=2) + "\n")


def signals(data: bytes) -> list[str]:
    return [name for name, literal in MARKERS.items() if literal in data]


def observations(path: Path, name: str) -> set[str]:
    if name == "test.outputs__outputs.zip":
        found = set()
        with zipfile.ZipFile(path) as bundle:
            for member in bundle.infolist():
                if member.file_size <= 16 << 20 and re.search(r"\.log(?:\.|$)|\.txt$|/logs/", member.filename):
                    found.update(signals(bundle.read(member)))
        return found
    if name == "test.log" or "/logs/" in name or name.endswith(".txt"):
        return set(signals(path.read_bytes()))
    return set()


def run(result: dict) -> None:
    OUT.mkdir()
    assert git("rev-parse", "HEAD").decode().strip() == SOURCE
    assert git("rev-parse", "HEAD^").decode().strip() == BASE
    for short, args in (("commit", ["cat-file", "commit", SOURCE]), ("before", ["diff", "HEAD", "--"])):
        (OUT / short).write_bytes(git(*args))
    assert not (OUT / "before").stat().st_size
    bazel = ["bazel", "--bazelrc=" + str(ROOT / "gnu.bazelrc")]
    selected = subprocess.run([*bazel, "query", "set(" + " ".join(TARGETS) + ")"], capture_output=True)
    result["query"] = {"exit": selected.returncode, "selected": sorted(set(selected.stdout.decode().splitlines()) & set(TARGETS))}
    save(result)
    assert selected.returncode == 0 and result["query"]["selected"] == sorted(TARGETS)
    args = ["test", "--config=x86_64", "--jobs=42", "--keep_going", "--nocache_test_results",
            "--invocation_id=" + result["invocation"], "--strategy=TestRunner=remote",
            "--modify_execution_info=^TestRunner$=+no-remote-cache", "--test_env=GTEST_FILTER=" + CASE,
            "--build_tag_filters=", "--test_tag_filters=", "--test_sharding_strategy=disabled",
            "--test_timeout=240", "--test_output=errors", "--remote_download_outputs=toplevel",
            "--build_event_json_file_path_conversion=false", "--build_event_json_file=" + str(OUT / "raw.json"),
            "--execution_log_json_file=" + str(OUT / "spawns.json"), *TARGETS]
    (OUT / "arguments").write_text("\n".join(args) + "\n")
    with (OUT / "bazel.log").open("wb") as log:
        code = subprocess.run(["timeout", "-k", "30s", "38m", *bazel, *args], stdout=log, stderr=log).returncode
    result.update(bazel_exit=code, execution_finished=now())
    save(result)
    (OUT / "exit").write_text(str(code))
    (OUT / "head").write_bytes(git("rev-parse", "HEAD"))
    (OUT / "after").write_bytes(git("diff", "HEAD", "--"))
    captured = reporter.capture(OUT / "raw.json", OUT / "bep.json", Path("/"), OUT / "reports")
    (OUT / "capture-exit").write_text(str(int(bool(captured["errors"]))))
    context = {"expected_source": SOURCE, "commit_file": "commit", "source_before": "before", "source_after": "after",
               "final_head": "head", "executed_arguments": "arguments", "native_exit": "exit",
               "capture_exit": "capture-exit", "command_kind": "test", "execution_finished_at": result["execution_finished"]}
    (OUT / "context.json").write_text(json.dumps(context))
    (OUT / "expected.txt").write_text("\n".join(TARGETS) + "\n")
    verdict = reporter.verdict(OUT / "bep.json", reporter.manifest_path(OUT / "bep.json"),
                               OUT / "context.json", OUT / "expected.txt")
    result["canonical"] = {"status": verdict["status"], "case_counts": verdict["case_counts"],
                           "errors": len(verdict["errors"]), "missing": len(verdict["missing"])}
    rows = result["owners"] = {label: {"actions": [], "reports": [], "signals": [], "cases": []} for label in TARGETS}
    save(result)
    for report in captured["reports"]:
        row = rows[report["identity"]["label"]]
        path = OUT / report["path"]
        name = report["name"]
        matched = observations(path, name)
        row["signals"] = sorted(set(row["signals"]) | matched)
        if name in ("test.xml", "test.log", "test.outputs__outputs.zip") or matched:
            row["reports"].append({"type": name if not name.startswith("test.outputs/") else "undeclared log",
                                   "sha256": report["sha256"], "bytes": report["bytes"],
                                   "signals": sorted(matched), "attempt": report["identity"]["attempt"]})
        if name == "test.xml":
            try:
                cases = list(ET.parse(path).iter("testcase"))
            except ET.ParseError:
                # A Sentry panic can interrupt XML emission. Keep collecting the
                # original process, steady-state controls and failure stack.
                cases = []
                row["incomplete_xml"] = True
            for case in cases:
                row["cases"].append({"matched": case.get("classname", "") + "." + case.get("name", "") == CASE,
                                     "failed": case.find("failure") is not None or case.find("error") is not None,
                                     "skipped": case.find("skipped") is not None or case.get("status") == "notrun"
                                     or case.get("result") == "suppressed"})
        save(result)
    for line in (OUT / "raw.json").open():
        event = json.loads(line)
        if "testResult" in event and (who := event["id"]["testResult"])["label"] in rows:
            row = rows[who["label"]]
            row.setdefault("attempts", []).append({"number": who.get("attempt", 1), "status": event["testResult"]["status"]})
    if (OUT / "spawns.json").is_file():
        with (OUT / "spawns.json").open() as stream:
            for _, action in execution_records(stream):
                if not action or action.get("targetLabel") not in rows or action.get("mnemonic") != "TestRunner":
                    continue
                args = action["commandArgs"]
                if Path(args[0]).name == "test-setup.sh":
                    label = action["targetLabel"]
                    props = {p["name"]: p["value"] for p in action["platform"]["properties"]}
                    rows[label]["actions"].append({"exit": action.get("exitCode", 0), "runner": action.get("runner"),
                                                  "digest": action.get("digest"), "cache": action.get("cacheHit", False),
                                                  "owner_matches": Path(args[1]).name == label.split(":")[1],
                                                  "platform_matches": all(props.get(k) == v for k, v in PROPERTIES.items())})
    save(result)
    assert not (OUT / "after").stat().st_size and (OUT / "head").read_text().strip() == SOURCE
    for row in rows.values():
        action, = row["actions"]
        assert action["runner"] == "remote" and not action["cache"] and action["owner_matches"] and action["platform_matches"]
    def passed(row: dict) -> bool:
        return (row["attempts"] == [{"number": 1, "status": "PASSED"}] and row["actions"][0]["exit"] == 0
                and row["cases"] == [{"matched": True, "failed": False, "skipped": False}]
                and {"start", "control_read", "queued_read"} <= set(row["signals"]))
    assert all(passed(rows[k]) for k in TARGETS[:2])
    last = rows[TARGETS[-1]]
    markers = set(last["signals"])
    assert "restore" in markers and {"start", "control_read"} <= markers
    if passed(last) and code == 0 and verdict["status"] == "PASS":
        result["status"] = "BOTH_TIMERS_REARMED_AFTER_RESTORE"
    else:
        assert code == 3 and last["actions"][0]["exit"] != 0
        assert last["attempts"] == [{"number": 1, "status": "FAILED"}]
        assert "queued_read" not in markers and {"SetWithClock", "Reset"} <= markers
        assert {"nil_pointer", "nil_timer"} & markers
        result["status"] = "QUEUED_TIMER_REARM_PANICKED_AFTER_RESTORE"


if __name__ == "__main__":
    result = {"source": SOURCE, "upstream": BASE, "case": CASE, "expected": TARGETS,
              "invocation": str(uuid.uuid4()), "started": now(), "status": "INCOMPLETE"}
    save(result)
    try:
        run(result)
    except Exception as err:
        result["collection_error"] = {"type": type(err).__name__, "line": traceback.extract_tb(err.__traceback__)[-1].lineno}
    result["verified_at"] = now()
    save(result)
    print(json.dumps(result))
    raise SystemExit(result["status"] == "INCOMPLETE")
