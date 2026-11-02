"""Exercise the case verifier before submitting the expensive consumer build."""

import hashlib
import json

import consumer


def main():
    private = consumer.TMP / "case-verifier-controls"
    private.mkdir()
    consumer.TMP = private
    consumer.save = lambda row: None
    target = consumer.TEST
    cases = [
        ("pass", 0, "PASSED", "<testcase name='TestConnectToSelf'/>", "PASS", 1),
        ("pass-with-digest", 0, "PASSED", "<testcase name='TestConnectToSelf'/>", "PASS", 1),
        ("failure", 3, "FAILED", "<testcase name='TestConnectToSelf'><failure/></testcase>", "FAIL", 1),
        ("skip", 0, "PASSED", "<testcase name='TestConnectToSelf' result='suppressed'/>", "INCOMPLETE", 0),
        ("zero", 0, "PASSED", "", "INCOMPLETE", 0),
        ("missing", 0, "PASSED", None, "INCOMPLETE", 0),
        ("failed-missing", 3, "FAILED", None, "FAIL", 0),
    ]
    for label, exit_code, status, xml, expected, count in cases:
        data = ("<testsuite>" + (xml or "") + "</testsuite>").encode()
        path = private / (label + ".xml")
        path.write_bytes(data)
        output = {"name": "test.xml", "uri": path.as_uri(), "digest": hashlib.sha256(data).hexdigest(), "length": str(len(data))}
        if label != "pass-with-digest":
            del output["digest"], output["length"]
        log = private / (label + ".log")
        log.write_text("Original test log fixture\n")
        result = {"status": status, "executionInfo": {"strategy": "local"}, "testActionOutput": [{"name": "test.log", "uri": log.as_uri()}] + ([output] if xml is not None else [])}
        if label == "pass":
            result.update(testAttemptStart="2026-11-02T01:00:00Z", testAttemptDuration="0.125s")
        if label == "pass-with-digest":
            result.update(testAttemptStartMillis="1793581200000", testAttemptDurationMillis="125")
        events = [{"id": {"started": {}}, "started": {"uuid": label}}, {"id": {"testResult": {"label": target, "configuration": {"id": "fixture"}, "run": 1, "shard": 1, "attempt": 1}}, "testResult": result}]
        (private / "socket.bep").write_text("\n".join(json.dumps(e) for e in events))
        consumer.command = lambda *args: exit_code
        row = {"status": "INCOMPLETE"}
        consumer.test_case(row, "socket", [], [], target, "TestConnectToSelf")
        assert row["socket"]["status"] == expected, label
        assert row["tests_executed"] == count, label
        if expected == "PASS":
            assert row["socket"]["timing_status"] == "PASS", label
            assert row["socket"]["execution_finished"].endswith("00.125000+00:00"), label
        if exit_code:
            assert row["status"] == "FAIL", label
    print("Seven verifier controls (including original local URI without digest metadata) passed; no product test executed.")


if __name__ == "__main__":
    main()
