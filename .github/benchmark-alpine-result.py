"""Read the original Alpine test outputs; this program never submits work."""

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tarfile
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path("test/rbe").resolve()))
import qualification_report as report

INVOCATION = "99bceafe-b6bb-4747-9b2b-2d006d795f8e"
SOURCE = "d6731da5ade2667a4077a0bac939afbccd92e8e4"
ARCHIVE = ("18498c56658ae2f6653022a2edab4c8617446cfaf64c80e3678eb3dce9659e40", 1008456)


def main() -> int:
    result = {"invocation": INVOCATION, "source": SOURCE, "archive_sha256": ARCHIVE[0],
              "archive_bytes": ARCHIVE[1], "owners": {}}
    key = os.environ.pop("BUILDBUDDY_API_KEY")
    root = Path(os.environ["RUNNER_TEMP"])
    with TemporaryDirectory(dir=root) as private, os.fdopen(os.memfd_create("alpine-retained"), "w+b") as fp:
        fp.write(key.encode())
        fp.flush()
        fp.seek(0)
        bundle = Path(private) / "original.tar.gz"
        receipt = report.native_receipt(INVOCATION, Path(f"/proc/self/fd/{fp.fileno()}"), bundle,
                                        Path(private) / "receipt.json", SOURCE, None, 240)
        if receipt.get("collection_status") != "COMPLETE" or (
                hashlib.sha256(bundle.read_bytes()).hexdigest(), bundle.stat().st_size) != ARCHIVE:
            raise ValueError("Original receipt or archive differs")
        result["original_status"] = receipt["status"]
        with tarfile.open(bundle) as archive:
            verdict = json.load(archive.extractfile("invocation-consumers/verdict.json"))
            result["consumer_verdict"] = {k: verdict[k] for k in
                ("status", "native_exit", "case_counts", "errors", "missing", "zero_case_owners",
                 "non_runtime_owners", "unpassed_required_cases") if k in verdict}
            for owner in ("startup_test", "usage_test"):
                selected = {}
                for name in ("test.log", "test.xml"):
                    matches = [m for m in archive if m.isfile() and m.name.startswith("invocation-consumers/reports/")
                               and m.name.endswith(f"/test/benchmarks/base/{owner}/{name}")]
                    if len(matches) != 1 or matches[0].size > 8 * 1024 * 1024:
                        raise ValueError(f"Expected one bounded {owner}/{name}")
                    data = archive.extractfile(matches[0]).read()
                    entry = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                    if name == "test.log":
                        # Only the known benchmark observations and Go's own
                        # PASS/no-tests markers may leave the private runner.
                        entry["lines"] = [line[:512] for line in data.decode().splitlines() if
                            re.search(r"Benchmark(?:Startup|Size)Empty(?:[-/\s]|$)", line) or
                            line.strip() in {"PASS", "testing: warning: no tests to run"} or
                            re.fullmatch(r"\s*1\s+\d+(?:[.]\d+)?\s+ns/op(?:\s+.*)?", line)][:20]
                    else:
                        xml = ET.fromstring(data)
                        entry["counts"] = [{k: e.attrib[k] for k in ("name", "tests", "failures", "errors", "skipped")
                                            if k in e.attrib} for e in xml.iter("testsuite")]
                        entry["testcases"] = [{k: e.attrib[k] for k in ("name", "status") if k in e.attrib}
                                              for e in xml.iter("testcase")][:12]
                    selected[name] = entry
                result["owners"][owner] = selected
    raw = (json.dumps(result, indent=2) + "\n").encode()
    if key.encode() in raw or b"x-buildbuddy-api-key" in raw.lower():
        raise ValueError("Unexpected credential in the bounded result")
    (root / "benchmark-alpine-retained.json").write_bytes(raw)
    print(f"Recovered original benchmark records from https://app.buildbuddy.io/invocation/{INVOCATION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
