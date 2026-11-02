"""Recover the original failed attempt and its passed controls before a gap retry."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from cas_inputs import blob, rpc
from tcp_mss import LABELS, PUBLIC, now, write

SOURCE = "94efbab6867cd16cac899786d5d8c05f0b326566"
ZIP_SHA = "4a0923ce5be5b749ea860ce2a3cacdd4ad6d7a46cc7f367d0d56daeb48febd41"
RESULT = {"status": "INCOMPLETE", "source": SOURCE, "original_run": 41424278605, "artifact": 13130973564, "archive_sha256": ZIP_SHA, "owners": {}}


def saved():
    write(PUBLIC / "original-recovery.json", RESULT)


def invocation_id(row):
    return row.get("invocationId") or row.get("invocation_id") or row.get("id", {}).get("invocationId") or row.get("id", {}).get("invocation_id")


def recover():
    path = Path(os.environ["RUNNER_TEMP"]) / "mss-original.zip"
    assert sha256(path.read_bytes()).hexdigest() == ZIP_SHA
    with ZipFile(path) as archive:
        assert archive.namelist() == ["result.json"]
        original = json.loads(archive.read("result.json"))
    assert original["status"] == "FAIL" and original["source"] == SOURCE and original["native_exit"] == 3
    assert original["selected"] == LABELS and original["qualification"]["case_counts"] == {"passed": 4, "failed": 4, "skipped": 0}
    write(PUBLIC / "original-result.json", original)
    # Select this exact pair within its retained command interval. A nonempty
    # source supplied by the service must match the original source receipt.
    short_pattern = LABELS[0] + " and 1 more"
    response = rpc("SearchInvocation", {"query": {"command": "test", "pattern": short_pattern, "updatedAfter": "2026-11-02T09:54:22Z", "updatedBefore": "2026-11-02T09:57:15Z"}, "count": 10})
    assert not response.get("nextPageToken")
    rows = response.get("invocation", [])
    matched = [row for row in rows if row.get("pattern") == [short_pattern]]
    RESULT["candidate_count"] = len(matched)
    assert len(matched) == 1
    selected = matched[0]
    assert selected.get("commitSha", "") in ("", SOURCE)
    invocation = invocation_id(selected)
    assert re.fullmatch(r"[a-f0-9-]{36}", invocation)
    full, = rpc("GetInvocation", {"lookup": {"invocationId": invocation}})["invocation"]
    assert invocation_id(full) == invocation and full["pattern"] == LABELS
    assert full.get("commitSha", "") in ("", SOURCE)
    RESULT["invocation"] = invocation
    RESULT["selection"] = {"host": selected.get("host"), "pattern": full["pattern"], "short_pattern": short_pattern, "commit_sha": selected.get("commitSha", ""), "created_at_usec": selected.get("createdAtUsec"), "updated_at_usec": selected.get("updatedAtUsec")}
    saved()
    for label in LABELS:
        arm = "native" if label.endswith("_native_test") else "netstack"
        row = RESULT["owners"][arm] = {"label": label, "status": "INCOMPLETE"}
        saved()
        try:
            entries = rpc("GetExecution", {"executionLookup": {"invocationId": invocation, "targetLabel": label}, "inlineExecuteResponse": True}).get("execution", [])
            action, = [entry for entry in entries if entry.get("actionMnemonic") == "TestRunner" and entry.get("targetLabel") == label]
            response = action["executeResponse"]
            result = response["result"]
            assert action["stage"] in (4, "COMPLETED") and result["exitCode"] == 1 and not response.get("cachedResult", False)
            row.update(action=action["actionDigest"], exit=1, cached=False, files={})
            contents = {}
            for name in ("test.log", "test.xml"):
                output, = [item for item in result.get("outputFiles", []) if Path(item["path"]).name == name]
                digest = output["digest"]
                data = blob(digest, invocation)
                row["files"][name] = {"path": output["path"], "digest": digest}
                (PUBLIC / f"original-{arm}-{name}").write_bytes(data)
                contents[name] = data
                saved()
            cases = list(ET.fromstring(contents["test.xml"]).iter("testcase"))
            assert len(cases) == 4 and len({case.get("name") for case in cases}) == 4
            dispositions = {case.get("name"): "FAIL" if case.find("failure") is not None or case.find("error") is not None else "SKIP" if case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "suppressed" else "PASS" for case in cases}
            assert dispositions == {"TestMSSSACKObservation": "FAIL", "TestMSSSACKObservation/sack_receive_gap": "FAIL", "TestMSSSACKObservation/no_sack": "PASS", "TestMSSSACKObservation/sack": "PASS"}
            row["cases"] = dispositions
            row["observations"] = [json.loads(raw) for raw in re.findall(rb"MSSOBS (\{[^\n]*\})", contents["test.log"])]
            assert len(row["observations"]) == 2 and {value["case"] for value in row["observations"]} == {"no_sack", "sack"}
            failures = ["".join(case.itertext()) for case in cases if case.get("name") == "TestMSSSACKObservation/sack_receive_gap"]
            row["gap_failure"] = failures
            row["expected_gap_cursor_failure"] = len(failures) == 1 and "data after 0 bytes:" in failures[0]
            row["status"] = "RECOVERED"
        except Exception as error:
            row["error"] = type(error).__name__
            trace = error.__traceback__
            while trace.tb_next:
                trace = trace.tb_next
            row["line"] = trace.tb_lineno
        saved()
    assert all(row["status"] == "RECOVERED" for row in RESULT["owners"].values())
    RESULT["status"] = "RECOVERED"
    if all(row["expected_gap_cursor_failure"] for row in RESULT["owners"].values()):
        (Path(os.environ["RUNNER_TEMP"]) / "mss-gap-ready").touch()


PUBLIC.mkdir(exist_ok=True)
try:
    recover()
except Exception as error:
    RESULT["error"] = type(error).__name__
    trace = error.__traceback__
    while trace.tb_next:
        trace = trace.tb_next
    RESULT["line"] = trace.tb_lineno
RESULT["verdict_at"] = now()
saved()
raise SystemExit(RESULT["status"] != "RECOVERED")
