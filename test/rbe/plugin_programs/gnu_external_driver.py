#!/usr/bin/env python3
"""Run the previously verified bridge fixture on the published GNU runtime."""

import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import xml.etree.ElementTree as ET


FIXTURE = "d7046c226c65c5e9243e0d70a940fe57ab27ffdae45a2451058b4f6764a42c9d"


def main():
    if sys.argv[1:2] == ["measure"]:
        fixture, destination, reuse = sys.argv[2:]
        assert hashlib.sha256(Path(fixture).read_bytes()).hexdigest() == FIXTURE
        api = runpy.run_path(fixture)
        runtimes = [api["RUNTIME"]] if reuse == "yes" else ["runc", api["RUNTIME"]]
        assert reuse in ("yes", "no")
        arms = {}
        for runtime in runtimes:
            row = arms[runtime] = {}
            try:
                api["observe"](runtime, row)
            except Exception as exc:
                row["error"] = api["error"](exc)
            Path(destination, "wire.json").write_text(json.dumps(arms, indent=2) + "\n")
        assert all(row.get("ok") for row in arms.values())
        return
    fixture = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"] / "test/rbe/plugin_programs/external_traffic.py"
    rc = subprocess.run(["sudo", "-n", sys.executable, str(Path(__file__).resolve()), "measure",
                         str(fixture), os.environ["REPORT_DIR"], os.environ["REUSE_RUNC"]]).returncode
    suite = ET.Element("testsuite", tests="1", failures=str(int(bool(rc))))
    case = ET.SubElement(suite, "testcase", name="TestGNUExternalTraffic")
    if rc:
        ET.SubElement(case, "failure", message="See the native test and per-protocol records.")
    ET.ElementTree(suite).write(os.environ["XML_OUTPUT_FILE"], encoding="utf-8")
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
