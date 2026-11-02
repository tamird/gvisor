"""Read the two completed original test attempts; never execute a test."""

import datetime
import hashlib
import json
import os
import re
import traceback
from urllib.parse import urlencode
import xml.etree.ElementTree as ET
import zipfile

from cas_inputs import fetch, rpc
from qualify import OUT, TMP, now, save
from resume_consumer import walk

SHA = "e94010aa005ae1ad5715826c54628a57bc9b77407f40e51fbe76e23fe8b21c5d"
SOURCE = "8a27540a0592bc1f88b954dd7b6ebdd3210cf10b"
CASES = {
    "backtrace": ("1dbfd7fa-8f68-491f-92dc-8e10f6f8feed", "//test/rbe/plugin_programs/reused:backtrace", "TestDumpStack"),
    "socket": ("3cc9ee63-45a2-4bb1-9e8b-c94bd77d8d46", "//test/rbe/plugin_programs/reused:socket", "TestConnectToSelf"),
}


def field(row, snake, camel):
    return row.get(snake, row.get(camel))


def original_xml(invocation, label, row):
    response = json.loads(fetch("api/v1/GetTarget", {"selector": {"invocation_id": invocation, "label": label}}))
    assert not field(response, "next_page_token", "nextPageToken")
    target, = response["target"]
    assert target["label"] == label and field(target["id"], "invocation_id", "invocationId") == invocation
    row["api_target_status"] = target["status"]
    if target["status"] in (3, 6, 7, 9, 11, "FAILED_TO_BUILD", "FAILED", "TIMED_OUT", "TOOL_FAILED", "FLAKY"):
        row["status"] = "FAIL"
    assert target["status"] in (5, "PASSED")  # api/v1/common.proto Status.
    response = json.loads(fetch("api/v1/GetAction", {"selector": {"invocation_id": invocation, "target_label": label}}))
    assert not field(response, "next_page_token", "nextPageToken")
    choices = []
    for action in response.get("action", []):
        assert field(action["id"], "invocation_id", "invocationId") == invocation
        assert field(action, "target_label", "targetLabel") == label
        choices.extend((action, f) for f in action.get("file", []) if f["name"] == "test.xml")
    action, file = choices[0] if len(choices) == 1 else (None, None)
    assert action is not None, "Expected exactly one original XML"
    match = re.fullmatch(r"bytestream://remote\.buildbuddy\.io/(?:[^?#]*/)?blobs/([0-9a-f]{64})/(\d+)", file["uri"])
    assert match, "Original XML has no CAS identity"
    digest, length = match.group(1), int(match.group(2))
    assert 0 < length < 1024 * 1024
    if file.get("hash"):
        assert file["hash"] == digest
    if field(file, "size_bytes", "sizeBytes") is not None:
        assert int(field(file, "size_bytes", "sizeBytes")) == length
    data = fetch("file/download?" + urlencode({"bytestream_url": file["uri"], "invocation_id": invocation}))
    assert len(data) == length and hashlib.sha256(data).hexdigest() == digest
    return data, {"sha256": digest, "bytes": length, "action_id": action["id"], "target_status": target["status"]}


def original_finish(invocation, label):
    response = rpc("GetInvocation", {"lookup": {"invocationId": invocation}})
    original, = response["invocation"]
    assert original["invocationId"] == invocation
    if original.get("commitSha"):
        assert original["commitSha"] == SOURCE
    event, = [v for v in walk(original) if isinstance(v.get("id"), dict)
              and v["id"].get("testResult", {}).get("label") == label and "testResult" in v]
    identity, result = event["id"]["testResult"], event["testResult"]
    assert all(int(identity[k]) == 1 for k in ("run", "shard", "attempt"))
    assert result["status"] in (1, "PASSED") and not result.get("cachedLocally")
    info = result["executionInfo"]
    assert info["strategy"] in ("local", "standalone") and not info.get("cachedRemotely")
    if result.get("testAttemptStart"):
        start = datetime.datetime.fromisoformat(result["testAttemptStart"].replace("Z", "+00:00"))
    else:
        start = datetime.datetime.fromtimestamp(int(result["testAttemptStartMillis"]) / 1000, datetime.timezone.utc)
    if result.get("testAttemptDuration"):
        duration = result["testAttemptDuration"]
        assert re.fullmatch(r"[0-9]+(?:\.[0-9]+)?s", duration)
        seconds = float(duration[:-1])
    else:
        seconds = int(result["testAttemptDurationMillis"]) / 1000
    assert seconds >= 0
    return {"start": start.isoformat(), "duration_seconds": seconds,
            "finish": (start + datetime.timedelta(seconds=seconds)).isoformat(),
            "source": "original BEP TestResult", "strategy": info["strategy"], "fresh": True}


def main():
    data = (TMP / "original.zip").read_bytes()
    assert hashlib.sha256(data).hexdigest() == SHA
    with zipfile.ZipFile(TMP / "original.zip") as z:
        assert set(z.namelist()) == {"result.json", "image.json"}
        r = json.loads(z.read("result.json"))
        (OUT / "image.json").write_bytes(z.read("image.json"))
    assert r["status"] == "INCOMPLETE" and r["recovery"]["carrier"] == SOURCE
    assert r["consumer_inputs"]["status"] == r["recovery"]["status"] == "PASS"
    assert r["runtime"]["network"] == "plugin" and all(r["runtime"]["observed"].values())
    assert r["tests_executed"] == r["case_records"] == 0
    r["named_recovery"] = {"run": 41371970540, "artifact": 13109108628, "sha256": SHA,
                           "original_status": r["status"], "started": now(), "carrier": os.environ["GITHUB_SHA"],
                           "builds_executed": 0, "tests_executed": 0}
    r["manual_interventions"].append("recover original named XML after absent local BEP digest fields")
    save(r)
    for key, (invocation, label, expected) in CASES.items():
        row = r[key]
        assert row["invocation"] == invocation and row["target"] == label and row["expected"] == expected
        assert row["exit"] == 0 and row["result"]["status"] == "PASSED"
        row["original_xml_collection_error"] = row.pop("collection_error")
        try:
            xml, identity = original_xml(invocation, label, row)
            cases = [{"class": c.get("classname"), "name": c.get("name"),
                      "status": "FAIL" if c.find("failure") is not None or c.find("error") is not None else
                                "SKIP" if c.find("skipped") is not None or c.get("status") == "notrun" or c.get("result") == "suppressed" else "PASS"}
                     for c in ET.fromstring(xml).iter("testcase")]
            row.update(cases=cases, xml_identity=identity, named_verdict_available=now())
            if any(c["status"] == "FAIL" for c in cases):
                row["status"] = r["status"] = "FAIL"
            save(r)
            assert len(cases) == 1 and cases[0]["name"] == expected and cases[0]["status"] == "PASS"
            row["status"] = "PASS"
            save(r)
            row["original_execution"] = original_finish(invocation, label)
            finish = datetime.datetime.fromisoformat(row["original_execution"]["finish"])
            available = datetime.datetime.fromisoformat(row["named_verdict_available"])
            row["execution_to_named_verdict_seconds"] = (available - finish).total_seconds()
            row["timing_status"] = "PASS"
        except Exception as exc:
            row["metadata_error"] = {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
        save(r)
    cases = [c for k in CASES for c in r[k].get("cases", [])]
    r.update(case_records=len(cases), tests_executed=sum(c["status"] != "SKIP" for c in cases),
             passed=sum(c["status"] == "PASS" for c in cases), failed=sum(c["status"] == "FAIL" for c in cases),
             skipped=sum(c["status"] == "SKIP" for c in cases))
    if all(r[k]["status"] == "PASS" for k in CASES):
        r["status"] = "PASS"
    elif any(r[k]["status"] == "FAIL" for k in CASES):
        r["status"] = "FAIL"
    r["named_recovery"].update(status=r["status"], finished=now(),
                              timing_status="PASS" if all(r[k].get("timing_status") == "PASS" for k in CASES) else "INCOMPLETE")
    r["final_verdict_time"] = now()
    save(r)
    return r["status"] == r["named_recovery"]["timing_status"] == "PASS"


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
