"""Fork-only comparison of rule-owned syscall worker memory budgets."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

import qualification_report as report
from stdlib_provenance import digest, fields, first, number


BASE = "183a39d1b2aff9e8e2219171940001749a2b5db0"
PRODUCT = "aa376e1b8025626f4ebca0d908cf8c9aee0a549f"
ROOT = Path(os.environ["GITHUB_WORKSPACE"])
TEMP = Path(os.environ["RUNNER_TEMP"])
OUT = TEMP / "memory-results"
BAZEL = [os.environ["BAZEL"], "--bazelrc=" + os.environ["BAZEL_RC"]]
PROCESS = "//test/syscalls:processes_test_runsc_ptrace"
MEMORY = "//test/syscalls:memory_accounting_test_runsc_systrap_directfs"
ARMS = {
    "process_default": (BASE, PROCESS, ""),
    "process_1gb": (PRODUCT, PROCESS, "1GB"),
    "memory_save_2gb": (PRODUCT, MEMORY + "_save", "2GB"),
    "memory_resume_2gb": (PRODUCT, MEMORY + "_save_resume", "2GB"),
}
OPTIONS = ["--config=x86_64", "--jobs=12", "--remote_download_outputs=all"]
SENSITIVE = re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|BEGIN (?:RSA |OPENSSH )?PRIVATE KEY|(?i:authorization|x-buildbuddy-api-key)\s*[:=]")


def now():
    return datetime.now(timezone.utc).isoformat()


def git(*args):
    assert Path.cwd() == ROOT
    return subprocess.check_output(["git", "-c", "safe.directory=" + str(ROOT), *args], text=True).strip()


def save(path, value):
    text = json.dumps(value, indent=2) + "\n"
    assert len(text.encode()) < 512 * 1024 and not SENSITIVE.search(text)
    path.write_text(text)


def label(value):
    return value.removeprefix("@@").removeprefix("@")


def run(args, log, seconds):
    with log.open("wb") as stream:
        try:
            return subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT, timeout=seconds, check=False).returncode
        except subprocess.TimeoutExpired:
            return 124


def classes(data):
    # These are observations, not inferred causes. No arbitrary log text leaves
    # this controller, including lines containing commands or checkpoint data.
    patterns = {
        "EAGAIN": rb"EAGAIN|Resource temporarily unavailable",
        "ENOMEM": rb"ENOMEM|Cannot allocate memory",
        "permission_denied": rb"Permission denied",
        "sandbox_bootstrap": rb"sandbox exited before the container started",
        "connection_reset": rb"connection reset by peer",
        "deadline": rb"DEADLINE_EXCEEDED|deadline exceeded",
        "fetch": rb"Error downloading|Failed to fetch|Failed to download",
        "missing_cc": rb"C compiler .cc. not found|exec: .cc.: executable file not found",
    }
    return [key for key, pattern in patterns.items() if re.search(pattern, data)]


def compact(path, target):
    result = {"invocation": None, "test_actions": [], "failed_actions": []}
    if not path.is_file():
        return result
    with subprocess.Popen(["zstd", "-qdc", str(path)], stdout=subprocess.PIPE) as proc:
        while (size := number(proc.stdout)) is not None:
            assert size < 64 * 1024**2
            raw = proc.stdout.read(size)
            assert len(raw) == size
            entry = fields(raw)
            if 2 in entry:
                result["invocation"] = first(fields(first(entry, 2)), 4).decode()
            if 7 not in entry:
                continue
            spawn = fields(first(entry, 7))
            owner = label(first(spawn, 7).decode())
            mnemonic = first(spawn, 8).decode()
            exit_code = first(spawn, 9, 0)
            status = first(spawn, 10)
            key = digest(first(spawn, 16))
            row = {"owner_sha256": hashlib.sha256(owner.encode()).hexdigest(), "mnemonic": mnemonic if mnemonic in {"TestRunner", "CppCompile", "CppLink", "GoCompilePkg", "GoLink", "GoStdlib", "GoStandardLibraryAnalysis"} else "other", "exit": exit_code, "status_empty": not status, "digest": key}
            if exit_code or status:
                result["failed_actions"].append(row)
            argv = [value.decode() for value in spawn.get(1, [])]
            if owner != target or mnemonic != "TestRunner" or not any(arg.endswith("/test-setup.sh") for arg in argv):
                continue
            props = {first(fields(value), 1).decode(): first(fields(value), 2).decode() for value in fields(first(spawn, 3)).get(1, [])}
            allowed = {"Arch", "OSFamily", "workload-isolation-type", "container-image", "dockerUser", "EstimatedMemory"}
            row.update(owner=owner, runner=first(spawn, 11).decode(), cached=bool(first(spawn, 12, 0)), properties={key: value for key, value in props.items() if key in allowed})
            result["test_actions"].append(row)
        assert proc.wait() == 0
    assert len(result["failed_actions"]) <= 100
    return result


def configured(source, arch):
    git("checkout", "--quiet", "--detach", source)
    work = TEMP / (source[:9] + "-" + arch)
    work.mkdir(exist_ok=True)
    output = work / "query.json"
    expr = 'filter("^//test/syscalls:(processes|memory_accounting)_test_", //test/syscalls:all)'
    with output.open("wb") as stdout, (work / "query.log").open("wb") as stderr:
        status = subprocess.run([*BAZEL, "cquery", expr, "--config=" + arch, "--output=jsonproto"], stdout=stdout, stderr=stderr, timeout=600, check=False).returncode
    assert status == 0, "configured owner selection failed"
    assert output.stat().st_size < 32 * 1024**2
    raw = json.loads(output.read_text())
    configurations = {str(item["id"]): item["checksum"] for item in raw["configurations"]}
    rows = {}
    for item in raw["results"]:
        rule = item["target"]["rule"]
        name = label(rule["name"])
        assert re.fullmatch(r"//test/syscalls:(?:processes|memory_accounting)_test_[a-z0-9_]+", name)
        attrs = {attr["name"]: attr for attr in rule["attribute"]}
        if rule["ruleClass"] != "_runner_test":
            continue
        assert name not in rows
        rows[name] = {"configuration": configurations[str(item["configurationId"])], "properties": {entry["key"]: entry["value"] for entry in attrs["exec_properties"].get("stringDictValue", [])}, "size": attrs["size"].get("stringValue"), "timeout": attrs["timeout"].get("stringValue"), "tags": attrs["tags"].get("stringListValue", []), "runner_args": attrs["runner_args"].get("stringListValue", []), "test": attrs["test"].get("stringValue")}
    assert set((PROCESS, MEMORY + "_save", MEMORY + "_save_resume")) <= rows.keys()
    assert "--platform=ptrace" in rows[PROCESS]["runner_args"]
    for suffix, option in (("_save", "--save=True"), ("_save_resume", "--save-resume=True")):
        assert option in rows[MEMORY + suffix]["runner_args"]
        assert "--platform=systrap" in rows[MEMORY + suffix]["runner_args"]
    return rows


def selection():
    assert git("rev-parse", PRODUCT + "^") == BASE
    assert git("diff", "--name-only", BASE, PRODUCT) == "test/syscalls/BUILD"
    result = {"status": "INCOMPLETE", "base": BASE, "product": PRODUCT, "architectures": {}}
    try:
        for arch in ("x86_64", "aarch64"):
            result["current"] = {"source": BASE, "architecture": arch}
            old = configured(BASE, arch)
            result["current"]["source"] = PRODUCT
            new = configured(PRODUCT, arch)
            assert old.keys() == new.keys()
            for name in old:
                assert old[name]["properties"] == {}
                assert new[name]["properties"] == {"test.EstimatedMemory": "1GB" if ":processes_" in name else "2GB"}
                assert {key: value for key, value in old[name].items() if key != "properties"} == {key: value for key, value in new[name].items() if key != "properties"}
            result["architectures"][arch] = {"analysis_only": True, "owner_count": len(old), "owners": [{"label": name, "configuration": old[name]["configuration"], "size": old[name]["size"], "timeout": old[name]["timeout"], "test": old[name]["test"], "old_properties": old[name]["properties"], "new_properties": new[name]["properties"], "unchanged_attributes_sha256": hashlib.sha256(json.dumps({key: value for key, value in old[name].items() if key != "properties"}, sort_keys=True).encode()).hexdigest()} for name in sorted(old)]}
        result["status"] = "PASS"
    except Exception as error:
        result["error_type"] = type(error).__name__
        raise
    finally:
        save(OUT / "selection.json", result)


def execute(arm):
    source, target, memory = ARMS[arm]
    git("checkout", "--quiet", "--detach", source)
    work = TEMP / ("memory-" + arm)
    work.mkdir(exist_ok=True)
    row = {"arm": arm, "source": source, "target": target, "target_architecture": "amd64", "execution_architecture": "amd64", "estimated_memory": memory or "unspecified", "status": "INCOMPLETE", "started_at": now()}
    row["requested_invocation"] = str(uuid.uuid4())
    raw = work / "events.json"
    expected_source = git("show", source + ":test/syscalls/linux/" + ("processes.cc" if target == PROCESS else "memory_accounting.cc"))
    expected = {suite + "." + case for suite, case in re.findall(r"^TEST\((\w+), (\w+)\)", expected_source, re.M)}
    assert expected and ("Processes.ThousandsOfThreads" if target == PROCESS else "MemoryAccounting.AnonAccountingPreservedOnSaveRestore") in expected
    # A requested UUID is a recovery address, not proof that Bazel started it.
    save(OUT / (arm + ".json"), row)
    try:
        outbase = subprocess.check_output([*BAZEL, "info", "output_base", *OPTIONS], text=True).strip()
        args = [*BAZEL, "test", target, *OPTIONS, "--invocation_id=" + row["requested_invocation"], "--nocache_test_results", "--test_timeout=600", "--test_output=errors", "--test_tag_filters=", "--build_event_json_file=" + str(raw), "--build_event_publish_all_actions", "--execution_log_compact_file=" + str(work / "execution.zst")]
        row["native_exit"] = run(args, work / "console.log", 2400 if arm == "process_default" else 1200)
        row["command_finished_at"] = now()
        save(OUT / (arm + ".json"), row)
        console = (work / "console.log").read_bytes()
        row["console"] = {"bytes": len(console), "sha256": hashlib.sha256(console).hexdigest(), "classes": classes(console)}
        events = [json.loads(line) for line in raw.read_text().splitlines()]
        results = [event for event in events if "testResult" in event]
        row["attempt_count"] = len(results)
        row["invocations"] = [event["started"]["uuid"] for event in events if "started" in event]
        row["test_results"] = [{"identity": event["id"]["testResult"], "status": event["testResult"]["status"], "cached_locally": event["testResult"].get("cachedLocally", False), "cached_remotely": event["testResult"].get("executionInfo", {}).get("cachedRemotely", False), "start": event["testResult"].get("testAttemptStart", {}), "duration": event["testResult"].get("testAttemptDuration", {})} for event in results]
        save(OUT / (arm + ".json"), row)
        capture = report.capture(raw, work / "clean.json", Path(outbase), work / "reports")
        row["capture_error_count"] = len(capture["errors"])
        row["reports"] = [{key: entry[key] for key in ("identity", "name", "bytes", "sha256")} for entry in capture["reports"]]
        cases = []
        logs = []
        for entry in capture["reports"]:
            path = work / entry["path"]
            if entry["name"] == "test.log":
                logs.append(path.read_bytes())
            if entry["name"] != "test.xml":
                continue
            report.xml_cases(path)
            for case in ET.parse(path).getroot().iter("testcase"):
                identity = case.get("classname", "") + "." + case.get("name", "")
                state = "skipped" if case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "suppressed" else "failed" if case.find("failure") is not None or case.find("error") is not None else "passed"
                cases.append({"name": identity if identity in expected else "unrecognized:" + hashlib.sha256(identity.encode()).hexdigest(), "status": state})
        row["cases"] = cases
        row["case_counts"] = dict(Counter(case["status"] for case in cases))
        row["log_classes"] = classes(b"\n".join(logs))
        assert len(results) == 1 and not row["capture_error_count"]
        event = results[0]
        detail = event["testResult"]
        if "testAttemptStartMillis" in detail and "testAttemptDurationMillis" in detail:
            row["execution_finished_at"] = datetime.fromtimestamp((int(detail["testAttemptStartMillis"]) + int(detail["testAttemptDurationMillis"])) / 1000, timezone.utc).isoformat()
        elif isinstance(detail.get("testAttemptStart"), str) and isinstance(detail.get("testAttemptDuration"), str):
            row["execution_finished_at"] = (datetime.fromisoformat(detail["testAttemptStart"].replace("Z", "+00:00")) + timedelta(seconds=float(detail["testAttemptDuration"].removesuffix("s")))).isoformat()
        row["placement"] = compact(work / "execution.zst", target)
        assert row["invocations"] == [row["requested_invocation"]]
        assert label(event["id"]["testResult"]["label"]) == target
        assert not event["testResult"].get("cachedLocally", False) and not event["testResult"].get("executionInfo", {}).get("cachedRemotely", False)
        actions = row["placement"]["test_actions"]
        assert len(actions) == 1
        action = actions[0]
        assert re.fullmatch(r"[a-f0-9]{64}", action["digest"][0]) and action["digest"][1] > 0
        assert action["runner"] == "remote" and not action["cached"]
        assert action["properties"].get("EstimatedMemory", "") == memory
        assert action["properties"]["Arch"] == "amd64" and action["properties"]["workload-isolation-type"] == "firecracker" and action["properties"]["dockerUser"] == "root"
        assert row["placement"]["invocation"] in row["invocations"]
        row["placement_verified"] = True
        assert Counter(case["name"] for case in cases) == Counter(expected)
        row["named_cases_complete"] = True
        if row["native_exit"] == 0 and event["testResult"]["status"] == "PASSED" and all(case["status"] == "passed" for case in cases) and action["exit"] == 0 and action["status_empty"]:
            row["status"] = "PASS"
        elif row["native_exit"] == 3 or event["testResult"]["status"] == "FAILED":
            row["status"] = "FAIL"
    except Exception as error:
        row["error_type"] = type(error).__name__
        if isinstance(error, FileNotFoundError):
            row["missing_file"] = "zstd" if error.filename == "zstd" else "build_events" if error.filename == str(raw) else "other"
        if row.get("native_exit") == 3 or row.get("case_counts", {}).get("failed", 0):
            row["status"] = "FAIL"
    finally:
        row["verified_at"] = now()
        save(OUT / (arm + ".json"), row)


def aggregate():
    result = {"status": "INCOMPLETE", "historical_receipt": "not recovered; historical sizing claims remain unverified", "arms": []}
    selection_path = OUT / "selection.json"
    if selection_path.exists():
        result["selection"] = json.loads(selection_path.read_text())
    for arm in ARMS:
        path = OUT / (arm + ".json")
        if path.exists():
            result["arms"].append(json.loads(path.read_text()))
    by_name = {row["arm"]: row for row in result["arms"]}
    baseline = by_name.get("process_default", {})
    if result.get("selection", {}).get("status") == "PASS" and all(by_name.get(arm, {}).get("status") == "PASS" for arm in ARMS if arm != "process_default") and baseline.get("status") in {"PASS", "FAIL"} and baseline.get("placement_verified") and baseline.get("named_cases_complete"):
        result["status"] = "PASS"
    elif any(row["status"] == "FAIL" for row in result["arms"] if row["arm"] != "process_default"):
        result["status"] = "FAIL"
    result["verified_at"] = now()
    save(OUT / "verdict.json", result)
    return result["status"] != "PASS"


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    mode = sys.argv[1]
    if mode == "selection":
        selection()
    elif mode == "aggregate":
        sys.exit(aggregate())
    else:
        execute(mode)
