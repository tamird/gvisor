"""Recover the original semaphore owner results before running missing owners."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request
import uuid
import xml.etree.ElementTree as ET

import qualification_report as reporter

SOURCE = "14a4e93a872bf6dd49893b6bd9a5f55517167e2b"
WORKFLOW = "4ea5a7772d5b18cb08ac2cf6aa7c3a96faca40f8"
OWNERS = [
    "//pkg/sentry/kernel/ipc:ipc_nogo",
    "//pkg/sentry/kernel:kernel_nogo",
    "//pkg/sentry/kernel/semaphore:semaphore_nogo",
    "//pkg/sentry/kernel/semaphore:semaphore_test",
    "//pkg/sentry/syscalls/linux:linux_nogo",
    "//pkg/sentry/socket/control:control_test",
]
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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(method, body, raw=False):
    path = "/rpc/BuildBuddyService/SearchInvocation" if method == "SearchInvocation" else "/api/v1/" + method
    req = urllib.request.Request(
        "https://app.buildbuddy.io" + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-buildbuddy-api-key": KEY},
    )
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=60) as response:
        content = response.read(32 * 1024 * 1024 + 1)
    assert len(content) <= 32 * 1024 * 1024
    return content if raw else json.loads(content)


def pages(method, body, field):
    result = []
    for _ in range(5):
        value = request(method, body)
        result.extend(value.get(field, []))
        token = value.get("nextPageToken", value.get("next_page_token"))
        if not token:
            return result
        body = dict(body, page_token=token)
    raise ValueError("Invocation metadata exceeded bounded pagination")


def value(item, camel, snake):
    return item.get(camel, item.get(snake))


def identifier(item):
    return str(uuid.UUID(value(item.get("id", item), "invocationId", "invocation_id")))


def xml_record(path):
    counts = reporter.xml_cases(path)
    names = ["/".join(filter(None, (case.get("classname"), case.get("name"))))
             for case in ET.parse(path).getroot().iter("testcase")]
    return {**reporter.file_record(path), "cases": counts, "names": names}


def recovered_target(invocation, label):
    targets = pages("GetTarget", {"selector": {"invocation_id": invocation, "label": label}}, "target")
    rows = [item for item in targets if item.get("label") == label]
    result = {"label": label, "status": "INCOMPLETE", "cache": "unknown: original command allowed cached tests", "reports": []}
    if len(rows) != 1:
        result["reason"] = "missing or ambiguous original target"
        return result
    original = rows[0]
    assert identifier(original) == invocation
    result["original_status"] = original.get("status")
    result["timing"] = original.get("timing", {})
    if original.get("status") in ("FAILED", "TIMED_OUT", "FAILED_TO_BUILD", "TOOL_FAILED", "FLAKY", 3, 6, 7, 9, 11):
        result["status"] = "FAIL"
    try:
        actions = pages("GetAction", {"selector": {"invocation_id": invocation, "target_label": label}}, "action")
        for index, action in enumerate(actions):
            assert value(action, "targetLabel", "target_label") == label
            assert identifier(action) == invocation
            for file in action.get("file", []):
                if file.get("name") != "test.xml":
                    continue
                uri = file.get("uri", "")
                match = re.fullmatch(r"bytestream://(?:remote|app)\.buildbuddy\.io/(?:[^?#\s]+/)?blobs/([0-9a-f]{64})/(\d+)", uri)
                if not match:
                    raise ValueError("Original XML lacks supported digest-bound CAS URI")
                content = request("GetFile", {"uri": uri}, raw=True)
                assert len(content) == int(match[2]) and hashlib.sha256(content).hexdigest() == match[1]
                assert not KEY or KEY.encode() not in content
                name = label[2:].replace("/", "_").replace(":", "_") + f"-{index}.xml"
                path = OUT / name
                path.write_bytes(content)
                result["reports"].append({"path": name, **xml_record(path)})
    except Exception as err:
        result["report_error_type"] = type(err).__name__
        return result
    if any(item["cases"]["failed"] for item in result["reports"]):
        result["status"] = "FAIL"
    elif (result["status"] != "FAIL" and original.get("status") in ("PASSED", 5)
          and result["reports"] and all(item["cases"]["passed"] > 0 for item in result["reports"])):
        result["status"] = "PASS"
    return result


def recover():
    row = {"status": "INCOMPLETE", "source": SOURCE, "workflow": WORKFLOW,
           "original_run": 41309783355, "original_artifact": 13089814511,
           "original_artifact_sha256": "12ec60d60ad308da2e726d538cb88034f3fe03862c100cbcc7c65539c31a9d4e",
           "expected": OWNERS, "queries": [], "owners": [], "started_at": now()}
    write(OUT / "original.json", row)
    candidates = {}
    for source in (SOURCE, WORKFLOW):
        try:
            items = pages("GetInvocation", {"selector": {"commit_sha": source}}, "invocation")
            row["queries"].append({"source": source, "count": len(items)})
            for item in items:
                candidates[identifier(item)] = item
        except Exception as err:
            row["queries"].append({"source": source, "error_type": type(err).__name__})
    def matches(items):
        selected = []
        for uid, item in items.items():
            pattern = item.get("pattern", "")
            patterns = pattern if isinstance(pattern, list) else pattern.split()
            created = int(value(item, "createdAtUsec", "created_at_usec") or 0)
            stamp = datetime.datetime.fromtimestamp(created / 1e6, datetime.timezone.utc).isoformat()
            if set(patterns) == set(OWNERS) and "2026-11-01T13:15:00" <= stamp < "2026-11-01T13:20:00":
                selected.append(uid)
        return selected

    if not matches(candidates):
        try:
            items = pages("SearchInvocation", {"query": {
                "pattern": " ".join(OWNERS), "command": "test",
                "updated_after": "2026-11-01T13:15:00Z", "updated_before": "2026-11-01T16:00:00Z",
            }, "count": 20}, "invocation")
            row["queries"].append({"pattern_search_count": len(items)})
            for item in items:
                candidates[identifier(item)] = item
        except Exception as err:
            row["queries"].append({"pattern_search_error": type(err).__name__})
    selected = matches(candidates)
    row["matching_invocations"] = selected
    if len(selected) == 1:
        uid = selected[0]
        original, = pages("GetInvocation", {"selector": {"invocation_id": uid}, "include_metadata": True}, "invocation")
        sha = value(original, "commitSha", "commit_sha")
        assert sha in (SOURCE, WORKFLOW), "Original source metadata does not match the fixed workflow"
        row["invocation"] = uid
        row["invocation_metadata"] = {key: original.get(key) for key in (
            "commitSha", "pattern", "success", "bazelExitCode", "invocationStatus", "createdAtUsec", "updatedAtUsec", "durationUsec")}
        for label in OWNERS:
            try:
                row["owners"].append(recovered_target(uid, label))
            except Exception as err:
                row["owners"].append({"label": label, "status": "INCOMPLETE", "error_type": type(err).__name__})
            write(OUT / "original.json", row)
    else:
        row["reason"] = "Original invocation unavailable or ambiguous"
    row["status"] = "FAIL" if any(item["status"] == "FAIL" for item in row["owners"]) else "PASS" if len(row["owners"]) == 6 and all(item["status"] == "PASS" for item in row["owners"]) else "INCOMPLETE"
    row["missing"] = [label for label in OWNERS if not any(item["label"] == label and item["status"] in ("PASS", "FAIL") for item in row["owners"])]
    row["verified_at"] = now()
    write(OUT / "original.json", row)
    (ROOT / "semop-missing.txt").write_text("\n".join(row["missing"]) + "\n")
    print("Original", row["status"], "missing owners", len(row["missing"]), flush=True)


def retry():
    missing = (ROOT / "semop-missing.txt").read_text().split()
    assert missing and set(missing) <= set(OWNERS) and len(set(missing)) == len(missing)
    original = json.loads((OUT / "original.json").read_text())
    assert missing == original["missing"]
    private = ROOT / "semop-owner-private"
    private.mkdir(exist_ok=True)
    def git(*args):
        return subprocess.check_output(["git", *args])
    assert git("rev-parse", "HEAD").decode().strip() == SOURCE
    (private / "commit").write_bytes(git("cat-file", "commit", SOURCE))
    (private / "before").write_bytes(git("diff", "HEAD", "--"))
    assert not (private / "before").read_bytes()
    args = ["test", "--config=x86_64", "--jobs=4", "--keep_going", "--build_tag_filters=", "--test_tag_filters=",
            "--noremote_local_fallback", "--strategy=TestRunner=remote", "--test_timeout=300", "--test_output=errors", "--nocache_test_results",
            "--modify_execution_info=^TestRunner$=+no-remote-cache", "--test_env=GO_TEST_WRAP_TESTV=1",
            "--remote_download_outputs=toplevel", "--remote_download_regex=.*[/]test[.](log|xml)$",
            "--build_event_json_file_path_conversion=false",
            "--build_event_json_file=" + str(private / "raw.json"), *missing]
    (private / "arguments").write_text("\n".join(args) + "\n")
    write(OUT / "retry.json", {"status": "INCOMPLETE", "source": SOURCE, "expected": missing, "started_at": now()})
    with (private / "bazel.log").open("wb") as log:
        rc = subprocess.run(["timeout", "--signal=INT", "--kill-after=30s", "20m", "bazel", "--noblock_for_lock",
                             "--bazelrc=" + str(ROOT / "tcp.bazelrc"), *args], stdout=log, stderr=log).returncode
    finished = now()
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
    for record in captured["reports"]:
        if record["name"] == "test.xml":
            source = private / record["path"]
            shutil.copyfile(source, OUT / ("retry-" + record["sha256"] + ".xml"))
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
        {"recover": recover, "retry": retry}[mode]()
    except Exception as err:
        write(OUT / (mode + "-error.json"), {"status": "INCOMPLETE", "error_type": type(err).__name__, "verified_at": now()})
        raise SystemExit(1)
