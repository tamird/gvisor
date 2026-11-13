#!/usr/bin/env python3
"""Check the original Java partition and the neighboring JMX operations."""
import csv
import json
import os
from pathlib import Path
import re
import subprocess

receipt = Path(os.environ["JAVA_RECEIPT"])
root = Path(os.environ["TEST_SRCDIR"]) / "_main"
prefix = "javax/management/remote/mandatory/connection/"
omitted = prefix + "BrokenConnectionTest.java"
neighbors = [prefix + name + ".java" for name in (
    "FailedConnectionTest", "CloseUnconnectedTest", "ConnectionListenerNullTest",
)]
runner, = (root / "test/runtimes/runner").glob("*/runner")
filename = root / "test/runtimes/exclude/java21.csv"
with filename.open() as stream:
    candidate = list(csv.reader(stream))
with (receipt / "parent.csv").open() as stream:
    parent = list(csv.reader(stream))
assert [row for row in candidate if row not in parent] == [
    [omitted, "https://bugs.openjdk.org/browse/JDK-8262312", "OpenJDK excludes this test on Linux"]
]
assert [row for row in candidate if row[0] != omitted] == parent
assert all(n not in {r[0] for r in candidate} for n in neighbors)
original = (receipt / "original.log").read_text()
def batch(text: str) -> list[str]:
    batches = re.findall(r"Running test case batch: ([^\r\n]+)", text)
    assert len(batches) == 1, batches
    return batches[0].strip().split(",")

previous = batch(original)
assert previous.count(omitted) == 1 and len(previous) == 22, previous
def inventory(output: str) -> set[str]:
    unselected, = re.findall(r"Tests not in this shard: ([^\r\n]+)", output)
    skips = re.findall(r"Skipping test case (\S+)", output)
    return set(unselected.split(",")) | set(batch(output)) | set(skips)

env = os.environ | {"RUNTIME": "ci-jmx", "PARTITION": "9", "TOTAL_PARTITIONS": "40",
                    "TEST_SHARD_INDEX": "2", "TEST_TOTAL_SHARDS": "8"}
def run_batch(name: str, exclusions: Path, extra: list[str], changes: dict[str, str]) -> tuple[str, int]:
    path = receipt / (name + ".log")
    with path.open("w") as out:
        proc = subprocess.run([str(runner), "--lang=java", "--image=java21", "--batch=100",
                               "--exclude_file=" + str(exclusions), *extra], cwd=root,
                              env=env | changes, stdout=out, stderr=subprocess.STDOUT, timeout=1500)
    text = path.read_text()
    print(name, proc.returncode, text[-400:], flush=True)
    return text, proc.returncode

stock, stock_status = run_batch("stock", receipt / "parent.csv", [], {})
actual, status = run_batch("candidate", filename, [], {})
before = batch(stock)
current = batch(actual)
assert before.count(omitted) == 1 and current == [t for t in before if t != omitted], (before, current)
assert status == 0 and f"{len(current)} tests passed" in actual, actual[-1250:]
assert "Skipping test case " + omitted in actual
# The supported control outcomes are a full passing batch or the same JMX failure as upstream.
assert stock_status == 0 or ("ListenerNotFoundException: Listener not found" in stock and
                             "Test results: passed: " + str(len(before)-1) + "; failed: 1" in stock), stock[-1300:]
product = inventory(actual)
upstream = inventory(original)
assert inventory(stock) == product
assert upstream - product == {"java/io/File/SetLastModified.java"} and not product - upstream
# Verify the exact 21 other cases from the upstream shard still execute despite different
# automatic sharding. --tests explicitly selects these names and bypasses the normal CSV.
archive_cases = [t for t in previous if t != omitted]
replayed, replay_status = run_batch("original-controls", filename, ["--batch=10000", "--tests=" + ",".join(archive_cases)],
                                    {"PARTITION": "1", "TOTAL_PARTITIONS": "1",
                                     "TEST_SHARD_INDEX": "0", "TEST_TOTAL_SHARDS": "1"})
assert replay_status == 0 and batch(replayed) == archive_cases and "21 tests passed" in replayed
(receipt / "partition.json").write_text(json.dumps({"upstreamBefore": previous, "upstreamControls": archive_cases,
    "forkBefore": before, "forkAfter": current, "stockExit": stock_status, "candidateExit": status,
    "originalControlsExit": replay_status, "upstreamCount": len(upstream), "forkCount": len(product),
    "upstreamOnly": sorted(upstream-product), "forkPartition": 9, "forkShard": 3}, indent=2) + "\n")

# These cases are in other partitions. Run their real jtreg actions and retain the complete log.
cmd = ["docker", "run", "--rm", "--runtime=ci-jmx", "gvisor.dev/images/runtimes/java21",
       "jtreg", "-agentvm", "-dir:/root/jdk/test/jdk", "-noreport", "-timeoutFactor:5",
       "-verbose:all", "-tl:200", *neighbors]
with (receipt / "neighbors.log").open("w") as out:
    proc = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT, timeout=900)
text = (receipt / "neighbors.log").read_text()
print(text[-900:], flush=True)
assert proc.returncode == 0, proc.returncode
for test in neighbors:
    assert re.search(r"^TEST: " + re.escape(test) + r"$[\s\S]*?^TEST RESULT: Passed\.", text, re.M), test
assert text.count("TEST RESULT: Passed.") == 3 and "Test results: passed: 3" in text
