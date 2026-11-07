"""Run the actual interpolated Buildkite commands against an argument/status recorder."""
import itertools
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


AMD = ["test", "--config=source-presubmit"]
ARM = ["test", "--config=source-presubmit-arm64"]
RECORDER = """#!/bin/sh
printf '%s\\0' "$#" "$@" >> "$RECORDED_CALLS"
case "$2" in
  --config=source-presubmit) exit "$AMD_EXIT" ;;
  --config=source-presubmit-arm64) exit "$ARM_EXIT" ;;
  *) exit 98 ;;
esac
"""


def execute(command, amd, arm):
    with tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"]) as tmp:
        work = Path(tmp)
        recorder = work / "bazel"
        recorder.write_text(RECORDER)
        recorder.chmod(0o755)
        history = work / "calls"
        result = subprocess.run(
            ["/bin/bash", "-e", "-c", command],
            env={"PATH": str(work), "RECORDED_CALLS": str(history),
                 "AMD_EXIT": str(amd), "ARM_EXIT": str(arm)},
            capture_output=True, text=True, timeout=10,
        )
        tokens = history.read_bytes().decode().split("\0")
        assert tokens.pop() == ""
        calls = []
        while tokens:
            count = int(tokens.pop(0))
            assert len(tokens) >= count and count >= 0
            calls.append(tokens[:count])
            del tokens[:count]
        return {"amd64": amd, "arm64": arm, "exit": result.returncode, "calls": calls}


class CoordinatorTest(unittest.TestCase):
    def test_both_profiles_and_exit_status(self):
        rows = []
        for amd, arm in itertools.product((0, 17), (0, 23)):
            with self.subTest(amd64=amd, arm64=arm):
                row = execute(os.environ["RENDERED_CANDIDATE"], amd, arm)
                self.assertEqual(row["calls"], [AMD, ARM])
                self.assertEqual(row["exit"], 1 if amd or arm else 0)
                rows.append(row)
        baseline = execute(os.environ["RENDERED_BASELINE"], 0, 23)
        self.assertEqual(baseline["calls"], [AMD])
        self.assertEqual(baseline["exit"], 0)
        unescaped = execute(os.environ["RENDERED_UNESCAPED"], 0, 0)
        self.assertEqual(unescaped["calls"], [AMD, ARM])
        self.assertEqual(unescaped["exit"], 47)
        print("PROFILE_RESULTS " + json.dumps({"candidate": rows, "stock": baseline, "unescaped": unescaped}, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
