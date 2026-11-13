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
env = os.environ | {"RUNTIME": "ci-jmx", "PARTITION": "9", "TOTAL_PARTITIONS": "40",
                    "TEST_SHARD_INDEX": "3", "TEST_TOTAL_SHARDS": "8"}
with (receipt / "candidate.log").open("w") as out:
    proc = subprocess.run([str(runner), "--lang=java", "--image=java21", "--batch=100",
                           "--exclude_file=" + str(filename)], cwd=root, env=env,
                           stdout=out, stderr=subprocess.STDOUT, timeout=2400)
actual = (receipt / "candidate.log").read_text()
print(actual[-2200:], flush=True)
assert proc.returncode == 0, proc.returncode
current = batch(actual)
assert current == [case for case in previous if case != omitted], current
assert "Skipping test case " + omitted in actual
assert re.search(r"21 tests passed", actual), actual[-1250:]
(receipt / "partition.json").write_text(json.dumps({"partition": 9, "partitions": 40, "shard": 4,
    "shards": 8, "before": previous, "after": current}, indent=2) + "\n")

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
