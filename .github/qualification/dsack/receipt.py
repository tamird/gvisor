"""Adapt the frozen sender's hidden input and publish only bounded verdict fields."""
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from uuid import UUID
import json
import os
import re
import runpy
import sys


SOURCE = "924c3e9f13ff3f7e7c888d3d70043076dfd7740e"
REQUEST = "dafdd52f7a5ed982274ca89746392c6bfd85a1279752d60d50f80f0e3911e12e"
OWNERS = ("//test/packetimpact/tests:tcp_rack_native_test", "//test/packetimpact/tests:tcp_rack_netstack_test")


def digest(s):
    if not isinstance(s, str) or not re.fullmatch(r"[0-9a-f]{64}", s):
        raise ValueError("Invalid digest")
    return s


def number(n, limit=2**32 - 1):
    if type(n) is not int or not 0 <= n <= limit:
        raise ValueError("Invalid number")
    return n


def enum(s):
    return s if s in ("PASS", "FAIL", "PASSED", "FAILED", "TIMEOUT", "INCOMPLETE", "COMPLETE") else "UNKNOWN"


def pair(x):
    if not isinstance(x, list) or len(x) != 2:
        raise ValueError("Invalid range")
    return [number(n) for n in x]


def public_result(z):
    if z["source"] != SOURCE or z["parent"] != "c451fa7d68e7b7809142c5daba0cd46a9cffe4fc":
        raise ValueError("Unexpected source")
    result = {"source": SOURCE, "request": REQUEST, "invocation": str(UUID(z["outer"])),
              "status": enum(z["status"]), "producer": enum(z["producer_status"]),
              "collection": enum(z["collection_status"]), "owners": {}}
    if z.get("child"):
        result["test_invocation"] = str(UUID(z["child"]))
    if z.get("bundle"):
        result["bundle_sha256"] = digest(z["bundle"]["sha256"])
        result["bundle_bytes"] = number(z["bundle"]["bytes"])
    delay = z.get("native_finish_to_verified_s")
    if type(delay) in (int, float) and 0 <= delay < 86_400:
        result["native_finish_to_verified_ms"] = round(delay * 1000, 3)
    for label in OWNERS:
        row = z["owners"].get(label, {})
        item = result["owners"][label] = {"status": enum(row.get("status")), "verified": row.get("producer", {}).get("verified") is True}
        for field in ("xml", "log"):
            if row.get(field):
                item[field] = {"sha256": digest(row[field]["sha256"]), "bytes": number(row[field]["bytes"])}
        if row.get("cases"):
            cases = row["cases"]
            if len(cases) != 1 or cases[0].get("name") != "TestDSACKAboveGap":
                raise ValueError("Unexpected XML cases")
            tags = [e["tag"] for e in cases[0]["errors"]]
            item["case"] = "TestDSACKAboveGap"
            item["failures"] = tags.count("failure") + tags.count("error")
            item["skipped"] = tags.count("skipped")
        if row.get("wire"):
            w = row["wire"]
            if len(w["frames"]) != 5:
                raise ValueError("Unexpected capture census")
            item["pcap"] = {"sha256": digest(w["sha256"]), "bytes": number(w["bytes"], 8 << 20)}
            item["observations"] = []
            for f in w["frames"]:
                if len(f["blocks"]) > 4:
                    raise ValueError("Invalid SACK block count")
                item["observations"].append({"sent": pair(f["sent"]), "ack": number(f["ack"]),
                                             "blocks": [pair(x) for x in f["blocks"]]})
    return result


class SafeOutput:
    """Pass through only the immutable original invocation URL."""
    def write(self, s):
        if re.fullmatch(r"https://app\.buildbuddy\.io/invocation/[0-9a-f-]{36}", s):
            print(s, file=sys.__stdout__, flush=True)
        return len(s)

    def flush(self):
        pass


def collect(key):
    root = Path(os.environ["RUNNER_TEMP"]) / "dsack-comparison"
    task = root / "task"
    sys.path.insert(0, str(task))
    public = {"source": SOURCE, "request": REQUEST, "stage": "submit"}
    rc = 1
    try:
        sender = runpy.run_path(str(task / "submit.py"))
        with redirect_stdout(SafeOutput()), redirect_stderr(SafeOutput()):
            rc = sender["main"](lambda _: key)
        key = None
        public["stage"] = "reconcile"
        with redirect_stdout(SafeOutput()), redirect_stderr(SafeOutput()):
            runpy.run_path(str(task / "reconcile.py"))
        public = public_result(json.loads((task / "outcome.json").read_text()))
    except BaseException:
        rc = 1
    finally:
        key = None
        submission = task / "submission.json"
        if submission.is_file():
            public["invocation"] = str(UUID(json.loads(submission.read_text())["invocationId"]))
        (root / "verdict.json").write_text(json.dumps(public, indent=2) + "\n")
    raise SystemExit(rc)
