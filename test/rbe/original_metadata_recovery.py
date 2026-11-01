"""Read original Make checks and semaphore owner metadata; never run a test."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import semop_owner_recovery as api

OUT = api.ROOT / "original-metadata-evidence"
OUT.mkdir(exist_ok=True)
MAKE_INVOCATION = "57ccdecd-07cf-4034-8ff6-298bce8a8031"
MAKE_LABEL = "@@make+//tests:make_smoke_test"
MAKE_CHECKS = ["version", "hello", "substref", "functions", "fail_propagation", "parallel"]
OWNERS = api.OWNERS


def stamp(usec):
    return datetime.datetime.fromtimestamp(int(usec or 0) / 1e6, datetime.timezone.utc).isoformat()


def invocation(uid):
    rows = api.pages("GetInvocation", {"selector": {"invocation_id": uid}}, "invocation")
    row, = rows
    assert api.identifier(row) == uid
    return row


def projected_invocation(row):
    return {"invocation": api.identifier(row), "success": row.get("success"),
            "exit": api.value(row, "bazelExitCode", "bazel_exit_code"),
            "state": api.value(row, "invocationStatus", "invocation_status"),
            "created": stamp(api.value(row, "createdAtUsec", "created_at_usec")),
            "updated": stamp(api.value(row, "updatedAtUsec", "updated_at_usec")),
            "duration_usec": api.value(row, "durationUsec", "duration_usec")}


def target(uid, label):
    rows = api.pages("GetTarget", {"selector": {"invocation_id": uid, "label": label}}, "target")
    row, = [row for row in rows if row.get("label") == label]
    assert api.identifier(row) == uid
    return row


def make_checks():
    result = {"status": "INCOMPLETE", "invocation": MAKE_INVOCATION, "target": MAKE_LABEL,
              "original_run": 41324780583, "original_artifact": 13089904045,
              "original_artifact_sha256": "eaf7deb1c44faab9ca19c1709e6b02c6dd99f3764cd70e1e7247c9c04fc29f1c",
              "product": "7dc8df3c4bb7da1bc04037fe9a2d641cb36cc5a4",
              "registry": "f8f5f201805db9b0c27571f7630cb8a37f3a78d6",
              "supplier": "b0b4b6c173c4312c14f3c5a427e9e3cb7271143c",
              "expected_checks": MAKE_CHECKS,
              "execution": "Original local TestRunner on populated Ubuntu; its builds used scratch RBE. No re-execution.",
              "execution_finished_at": "2026-11-01T16:11:21.512896+00:00",
              "original_collection_status": "INCOMPLETE"}
    api.write(OUT / "make.json", result)
    try:
        original = invocation(MAKE_INVOCATION)
        sha = api.value(original, "commitSha", "commit_sha")
        assert sha in (None, "", result["product"], "42f86d73f5d17a653d4592dc158707cfba42089e")
        result["reported_commit"] = sha
        result["invocation_metadata"] = projected_invocation(original)
        row = target(MAKE_INVOCATION, MAKE_LABEL)
        result["target_status"] = row.get("status")
        result["timing"] = row.get("timing", {})
        if row.get("status") in (3, 6, 7, 9, 11, "FAILED_TO_BUILD", "FAILED", "TIMED_OUT", "TOOL_FAILED", "FLAKY"):
            result["status"] = "FAIL"
        api.write(OUT / "make.json", result)
        actions = api.pages("GetAction", {"selector": {"invocation_id": MAKE_INVOCATION, "target_label": MAKE_LABEL}}, "action")
        files = []
        for action in actions:
            assert api.identifier(action) == MAKE_INVOCATION
            assert api.value(action, "targetLabel", "target_label") == MAKE_LABEL
            files.extend(file for file in action.get("file", []) if file.get("name") == "test.log")
        file, = files
        uri = file.get("uri", "")
        match = re.fullmatch(r"bytestream://(?:remote|app)\.buildbuddy\.io/(?:[^?#\s]+/)?blobs/([0-9a-f]{64})/(\d+)", uri)
        assert match
        content = api.request("GetFile", {"uri": uri}, raw=True)
        assert len(content) == int(match[2]) and hashlib.sha256(content).hexdigest() == match[1]
        text = content.decode()
        passed = re.findall(r"^\[PASS\] (\w+)\s*$", text, re.MULTILINE)
        failed = re.findall(r"^\[FAIL\] (\w+)\s*$", text, re.MULTILINE)
        result["log"] = {"sha256": match[1], "bytes": len(content)}
        result["passed_checks"] = [name for name in passed if name in MAKE_CHECKS]
        result["failed_checks"] = [name for name in failed if name in MAKE_CHECKS]
        result["summary_matches"] = len(re.findall(r"^6/6 tests passed\s*$", text, re.MULTILINE)) == 1
        if failed:
            result["status"] = "FAIL"
        elif result["status"] != "FAIL" and row.get("status") in (5, "PASSED") and passed == MAKE_CHECKS and result["summary_matches"]:
            result["status"] = "PASS"
    except Exception as err:
        result["collection_error_type"] = type(err).__name__
    result["verified_at"] = api.now()
    api.write(OUT / "make.json", result)
    return result["status"]


def original_progress(uid):
    """Retain only typed counts and known failure classes from original logs."""
    body = {"selector": {"invocation_id": uid}}
    chunks = []
    complete = False
    for _ in range(5):
        row = api.request("GetLog", body)
        log = row.get("log", {})
        assert api.identifier(log) == uid
        chunks.append(log.get("contents", ""))
        token = api.value(row, "nextPageToken", "next_page_token")
        if not token:
            complete = True
            break
        body = dict(body, page_token=token)
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", "".join(chunks))
    progress = re.findall(r"\[([\d,]+) / ([\d,]+)\]", text)
    actions = re.findall(r"\] (GoCompilePkg|Nogo|GoLink|GoStdlib) (pkg/sentry/[A-Za-z0-9_./+-]+); ([0-9.]+)(s|m) ([a-z, -]+)", text)
    return {"sha256": hashlib.sha256(text.encode()).hexdigest(), "bytes": len(text.encode()), "complete": complete,
            "last_action_counts": [int(x.replace(",", "")) for x in progress[-1]] if progress else None,
            "last_owner_progress": [{"mnemonic": m, "path": p, "elapsed": float(t), "unit": u, "strategy": s.strip()} for m, p, t, u, s in actions[-6:]],
            "known_diagnostics": [label for label, pattern in (
                ("interrupted", r"(?i)(?:build interrupted|interrupting command|interrupted by user)"),
                ("out_of_memory", r"(?i)(?:out of memory|java.lang.OutOfMemoryError)"),
                ("remote_unavailable", r"(?:UNAVAILABLE|DEADLINE_EXCEEDED):"),
                ("missing_executable", r"(?:exec|executable).*No such file or directory"),
            ) if re.search(pattern, text)]}


def semaphore(kind, host, earliest, latest):
    result = {"status": "INCOMPLETE", "source": api.SOURCE, "expected": OWNERS,
              "scope": kind, "original_workflow": api.WORKFLOW, "queries": [], "owners": []}
    path = OUT / (kind + ".json")
    api.write(path, result)
    try:
        rows = api.pages("SearchInvocation", {"query": {"pattern": api.STORED_PATTERN, "command": "test", "host": host,
                         "updated_after": earliest + "Z", "updated_before": latest + "Z"}, "count": 20}, "invocation")
        selected = []
        for row in rows:
            pattern = row.get("pattern", "")
            patterns = pattern if isinstance(pattern, list) else [pattern]
            known = patterns == [api.STORED_PATTERN] or patterns == OWNERS
            created = stamp(api.value(row, "createdAtUsec", "created_at_usec"))
            result["queries"].append({**projected_invocation(row), "host_matches": row.get("host") == host,
                                      "pattern_matches": known, "pattern_count": len(patterns),
                                      "known_target_patterns": [label for label in patterns if label in OWNERS]})
            if known and row.get("host") == host and earliest <= created < latest:
                selected.append(api.identifier(row))
        uid, = selected
        row = invocation(uid)
        assert row.get("host") == host
        sha = api.value(row, "commitSha", "commit_sha")
        assert sha in (None, "", api.SOURCE, api.WORKFLOW)
        result["invocation_metadata"] = projected_invocation(row)
        result["source_binding"] = {"reported_commit": sha, "basis": "Exact original Docker host, six-owner command, job lifetime and immutable product checkout"}
        targets = api.pages("GetTarget", {"selector": {"invocation_id": uid}}, "target")
        assert {row["label"] for row in targets} <= set(OWNERS)
        for label in OWNERS:
            owner = api.recovered_target(uid, label)
            result["owners"].append(owner)
            for report in owner.get("reports", []):
                source = api.OUT / report["path"]
                (OUT / (kind + "-" + source.name)).write_bytes(source.read_bytes())
            api.write(path, result)
        result["status"] = "FAIL" if any(x["status"] == "FAIL" for x in result["owners"]) else "PASS" if all(x["status"] == "PASS" for x in result["owners"]) else "INCOMPLETE"
        try:
            result["progress"] = original_progress(uid)
        except Exception as err:
            result["progress_error_type"] = type(err).__name__
    except Exception as err:
        if any(x["status"] == "FAIL" for x in result["owners"]):
            result["status"] = "FAIL"
        result["collection_error_type"] = type(err).__name__
    result["verified_at"] = api.now()
    api.write(path, result)
    return result["status"]


def main():
    make = make_checks()
    original = semaphore("semaphore-original", "d92c44459829", "2026-11-01T13:08:52", "2026-11-01T16:00:00")
    retry = semaphore("semaphore-retry", "3030218f065c", "2026-11-01T16:08:45", "2026-11-01T16:35:00")
    api.write(OUT / "summary.json", {"make": make, "semaphore_original": original, "semaphore_retry": retry,
                                   "new_test_invocations": 0, "verified_at": api.now()})
    return make != "PASS"


if __name__ == "__main__":
    sys.exit(main())
