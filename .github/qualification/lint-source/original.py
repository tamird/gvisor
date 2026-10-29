"""Replay the final projection on the exact retained reports; never run Bazel."""
import json
import os
from hashlib import sha256
from pathlib import Path
import runpy
import xml.etree.ElementTree as ET

source = Path(__file__).with_name("owners.py")
settle = runpy.run_path(str(source))["settle"]
fetch = runpy.run_path(os.environ["ORIGINAL_READER"])["get"]
expected = {
    "good": "4a70bbc23e274997e30c01e2b196691fd545c7db8a5b4fef8a9c303eec9f12ee",
    "bad": "53f8be35285f6e565e5c0805ff3b33ca1c649d3b8de28caaa01372eba0c4599b",
}
token = os.environ.pop("BUILDBUDDY_API_KEY")
work = Path(os.environ["RUNNER_TEMP"])
store = {}
out = {}
for mode, digest in expected.items():
    raw = (work / mode / "result.json").read_bytes()
    assert sha256(raw).hexdigest() == digest
    data = json.loads(raw)
    original_status = data["status"]
    settle(data)
    rows = {}
    for label, row in data["tests"].items():
        files = {}
        for name, item in row["reports"].items():
            ident = (item["sha256"], item["bytes"])
            assert name in ("test.xml", "test.log") and ident[1] <= 65536
            if ident not in store:
                store[ident] = fetch(token, ident)
            files[name] = store[ident]
        root = ET.fromstring(files["test.xml"])
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        assert suites
        failures = sum(int(s.get("errors", 0)) + int(s.get("failures", 0)) for s in suites)
        assert bool(failures) == (row["status"] == "FAILED")
        assert row["include_order_control"] == (b"test/util/cpplint_order_control.cc:3:" in files["test.log"])
        rows[label] = {"status": row["status"], "xml_sha256": row["reports"]["test.xml"]["sha256"],
                       "xml_errors_or_failures": failures, "log_sha256": row["reports"]["test.log"]["sha256"]}
    out[mode] = {"source": data["source"], "tree": data["tree"], "original_status": original_status,
                 "original_sha256": digest, "replayed_status": data["status"],
                 "hits": sum(r["remote_cache"] for r in data["tests"].values()), "rows": rows}
    (work / "replay.json").write_text(json.dumps(out, sort_keys=True) + "\n")
assert (out["good"]["replayed_status"], out["bad"]["replayed_status"]) == ("PASS", "EXPECTED_REJECTION")
