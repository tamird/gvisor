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
        result = {"status": status, "executionInfo": {"strategy": "local"}, "testActionOutput": [output] if xml is not None else []}
        events = [{"id": {"started": {}}, "started": {"uuid": label}}, {"id": {"testResult": {"label": target}}, "testResult": result}]
        (private / "socket.bep").write_text("\n".join(json.dumps(e) for e in events))
        consumer.command = lambda *args: exit_code
        row = {"status": "INCOMPLETE"}
        consumer.test_case(row, "socket", [], [], target, "TestConnectToSelf")
        assert row["socket"]["status"] == expected, label
        assert row["tests_executed"] == count, label
        if exit_code:
            assert row["status"] == "FAIL", label
    print("Six verifier controls passed; no product test executed.")


if __name__ == "__main__":
    main()
